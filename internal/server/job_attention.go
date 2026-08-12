package server

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type jobAttention struct {
	Status      string            `json:"status"`
	NeedsAction bool              `json:"needs_action"`
	Issue       *attentionIssue   `json:"issue,omitempty"`
	Solutions   []attentionAction `json:"solutions"`
}

type attentionIssue struct {
	Code        string `json:"code"`
	Title       string `json:"title"`
	Summary     string `json:"summary"`
	CurrentStep string `json:"current_step,omitempty"`
	SiteCode    string `json:"site_code,omitempty"`
	Severity    string `json:"severity"`
}

type attentionAction struct {
	ID                   string `json:"id"`
	Label                string `json:"label"`
	Description          string `json:"description"`
	Kind                 string `json:"kind"`
	Executable           bool   `json:"executable"`
	RequiresConfirmation bool   `json:"requires_confirmation"`
	Href                 string `json:"href,omitempty"`
}

type jobActionRequest struct {
	ActionID            string             `json:"action_id"`
	ExpectedStatus      workflow.JobStatus `json:"expected_status"`
	ExpectedStep        string             `json:"expected_step,omitempty"`
	ExpectedBlockerCode string             `json:"expected_blocker_code,omitempty"`
	Confirmed           bool               `json:"confirmed,omitempty"`
}

type attentionBlocker struct {
	Code     string `json:"code"`
	Message  string `json:"message"`
	SiteCode string `json:"site_code,omitempty"`
}

func (a jobsAPI) attention(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "jobs:read"); !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": job.Status, "job_id": job.ID, "attention": attentionForJob(job),
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
	})
}

func (a jobsAPI) performAction(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "jobs:write")
	if !ok {
		return
	}
	id, ok := jobID(w, r)
	if !ok {
		return
	}
	var request jobActionRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	job, err := a.service.GetJob(r.Context(), id)
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	current := attentionForJob(job)
	blockerCode := ""
	if current.Issue != nil {
		blockerCode = current.Issue.Code
	}
	if request.ExpectedStatus != job.Status || request.ExpectedStep != job.CurrentStep || request.ExpectedBlockerCode != blockerCode {
		writeProblem(w, http.StatusConflict, "job_attention_changed", "job state changed; refresh the current issue before applying an action")
		return
	}
	var selected *attentionAction
	for index := range current.Solutions {
		if current.Solutions[index].ID == request.ActionID {
			selected = &current.Solutions[index]
			break
		}
	}
	if selected == nil || !selected.Executable {
		writeProblem(w, http.StatusConflict, "job_action_not_available", "the requested action is not executable for the current issue")
		return
	}
	if selected.RequiresConfirmation && !request.Confirmed {
		writeProblem(w, http.StatusBadRequest, "job_action_confirmation_required", "confirmed=true is required for this repair action")
		return
	}
	switch selected.ID {
	case "resume_job", "retry_step", "approve_safe_repair":
		job, err = a.service.ResumeJob(r.Context(), id, job.ResumeState, workflow.Actor{Type: "user", ID: principal.UserID})
	default:
		writeProblem(w, http.StatusConflict, "job_action_not_available", "the requested action has no approved executor")
		return
	}
	if err != nil {
		writeWorkflowError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": jobOK(job.Status), "status": job.Status, "job_id": job.ID,
		"applied_action": selected.ID, "attention": attentionForJob(job), "job": redactJob(job),
		"blockers": redactJSON(job.Blockers), "next_actions": redactJSON(job.NextActions),
	})
}

func attentionForJob(job workflow.Job) jobAttention {
	blockers := []attentionBlocker{}
	_ = json.Unmarshal(job.Blockers, &blockers)
	if len(blockers) > 0 {
		return attentionForBlocker(job, blockers[0])
	}
	switch job.Status {
	case workflow.JobPaused:
		return jobAttention{Status: "action_required", NeedsAction: true,
			Issue:     &attentionIssue{Code: "job_paused", Title: "任务已暂停", Summary: "继续后会从当前步骤恢复，不会跳过任何安全门禁。", CurrentStep: job.CurrentStep, Severity: "info"},
			Solutions: []attentionAction{{ID: "resume_job", Label: "继续任务", Description: "使用已保存的恢复状态继续当前步骤。", Kind: "retry", Executable: true}},
		}
	case workflow.JobFailed:
		return jobAttention{Status: "action_required", NeedsAction: true,
			Issue:     &attentionIssue{Code: "step_execution_failed", Title: "当前步骤执行失败", Summary: "系统没有得到可确认的成功结果；可以显式同意重置当前步骤并重试。", CurrentStep: job.CurrentStep, Severity: "error"},
			Solutions: []attentionAction{{ID: "approve_safe_repair", Label: "同意修复并重试", Description: "只重置本地步骤状态并保留审计证据，不修改规则、凭据或上传确认。", Kind: "safe_repair", Executable: true, RequiresConfirmation: true}},
		}
	case workflow.JobComplete, workflow.JobCancelled:
		return jobAttention{Status: string(job.Status), Solutions: []attentionAction{}}
	default:
		return jobAttention{Status: "working", Solutions: []attentionAction{}}
	}
}

func attentionForBlocker(job workflow.Job, blocker attentionBlocker) jobAttention {
	issue := &attentionIssue{Code: blocker.Code, Title: "任务需要处理", Summary: blocker.Message, CurrentStep: job.CurrentStep, SiteCode: blocker.SiteCode, Severity: "warning"}
	actions := []attentionAction{}
	code := strings.ToLower(blocker.Code)
	switch {
	case strings.Contains(code, "outcome_unknown") || strings.Contains(code, "reconciliation"):
		issue.Title = "远端结果未知，必须先对账"
		actions = append(actions, attentionAction{ID: "provide_reconciliation", Label: "填写对账结果", Description: "核对远端真实状态并提交绑定当前尝试的证据；系统不会盲目重试外部写入。", Kind: "manual_input"})
	case code == "confirm_upload_required":
		issue.Title = "等待最终发布确认"
		actions = append(actions, attentionAction{ID: "review_upload_package", Label: "审核发布内容", Description: "检查发布预览、最终查重与规则后，再显式确认 live 上传。", Kind: "manual_input"})
	case strings.Contains(code, "duplicate"):
		issue.Title = "目标站发现重复资源"
		actions = append(actions, attentionAction{ID: "review_duplicate_evidence", Label: "查看查重证据", Description: "重复门禁不能自动绕过；请查看候选并决定停止任务。", Kind: "manual_review"})
	case strings.Contains(code, "rule") || strings.Contains(code, "obligation") || strings.Contains(code, "accept_rules"):
		issue.Title = "站点规则尚未满足"
		actions = append(actions, attentionAction{ID: "open_site_rules", Label: "前往规则审核", Description: "完成缺失的规则提取、审批、激活或人工义务。", Kind: "navigate", Href: "/app/configuration/rules"})
	case strings.Contains(code, "credential") || strings.Contains(code, "api_key") || strings.Contains(code, "authentication") || strings.Contains(code, "site_configuration") || strings.Contains(code, "site_access"):
		issue.Title = "站点配置需要修正"
		actions = append(actions, attentionAction{ID: "open_site_configuration", Label: "前往站点配置", Description: "修正凭据、访问策略或站点配置后再重试。", Kind: "navigate", Href: "/app/configuration/rules"})
	case strings.Contains(code, "seeding") || strings.Contains(code, "seed_"):
		issue.Title = "做种要求尚未满足"
		actions = append(actions, attentionAction{ID: "review_seeding", Label: "查看做种状态", Description: "做种义务不能豁免；修正下载器状态后可重新核验。", Kind: "manual_review"})
	default:
		actions = append(actions, attentionAction{ID: "retry_step", Label: "重试当前步骤", Description: "保留已有证据并重新执行当前步骤；所有规则与外部写入门禁仍然生效。", Kind: "retry", Executable: true})
	}
	return jobAttention{Status: "action_required", NeedsAction: true, Issue: issue, Solutions: actions}
}
