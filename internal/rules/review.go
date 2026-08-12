package rules

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var reviewSectionOrder = []string{
	"upload_limit", "download_limit", "naming",
}

type ReviewCheck struct {
	Section     string    `json:"section"`
	Decision    string    `json:"decision"`
	Comment     string    `json:"comment,omitempty"`
	Fingerprint string    `json:"fingerprint"`
	ReviewerID  string    `json:"reviewer_id"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type ReviewSection struct {
	Key     string         `json:"key"`
	Title   string         `json:"title"`
	Status  string         `json:"status"`
	Summary string         `json:"summary"`
	Facts   []ReviewFact   `json:"facts"`
	Data    map[string]any `json:"data"`
	Check   *ReviewCheck   `json:"check,omitempty"`
}

type ReviewFact struct {
	Label  string `json:"label"`
	Value  string `json:"value"`
	Detail string `json:"detail,omitempty"`
	Tone   string `json:"tone,omitempty"`
}

type ReviewWorkspace struct {
	RevisionID     string           `json:"revision_id"`
	SiteCode       string           `json:"site_code"`
	Fingerprint    string           `json:"fingerprint"`
	RevisionStatus string           `json:"revision_status"`
	ApprovalReady  bool             `json:"approval_ready"`
	ConfirmedCount int              `json:"confirmed_count"`
	RequiredCount  int              `json:"required_count"`
	Sections       []ReviewSection  `json:"sections"`
	Advisories     []Advisory       `json:"advisories"`
	Blockers       []map[string]any `json:"blockers"`
	NextActions    []map[string]any `json:"next_actions"`
}

func (s *Store) GetReview(ctx context.Context, revisionID string) (ReviewWorkspace, error) {
	revision, err := s.Get(ctx, revisionID)
	if err != nil {
		return ReviewWorkspace{}, err
	}
	return s.reviewWorkspace(ctx, revision)
}

func (s *Store) SetReviewCheck(ctx context.Context, revisionID, section, fingerprint, decision, comment string, actor workflow.Actor) (ReviewWorkspace, error) {
	section = strings.ToLower(strings.TrimSpace(section))
	if !validReviewSection(section) {
		return ReviewWorkspace{}, fmt.Errorf("invalid review section %q", section)
	}
	if decision != "confirmed" && decision != "needs_changes" {
		return ReviewWorkspace{}, fmt.Errorf("review decision must be confirmed or needs_changes")
	}
	reviewerID, err := uuid.Parse(actor.ID)
	if err != nil {
		return ReviewWorkspace{}, fmt.Errorf("reviewer must be an authenticated user")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return ReviewWorkspace{}, fmt.Errorf("begin rule review transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	revision, err := scanRevision(tx.QueryRow(ctx, revisionSelect+" WHERE sr.id = $1 FOR UPDATE", revisionID))
	if err != nil {
		return ReviewWorkspace{}, err
	}
	if revision.Status != "draft" {
		return ReviewWorkspace{}, fmt.Errorf("%w: only draft revisions can be reviewed", ErrConflict)
	}
	if fingerprint != revision.Fingerprint {
		return ReviewWorkspace{}, fmt.Errorf("%w: rule fingerprint does not match", ErrConflict)
	}
	if decision == "needs_changes" && strings.TrimSpace(comment) == "" {
		return ReviewWorkspace{}, fmt.Errorf("comment is required when a section needs changes")
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO site_rule_review_checks(rule_revision_id, section, decision, comment, fingerprint, reviewer_id)
		VALUES ($1, $2, $3, NULLIF($4, ''), $5, $6)
		ON CONFLICT (rule_revision_id, section) DO UPDATE SET
			decision = EXCLUDED.decision, comment = EXCLUDED.comment,
			fingerprint = EXCLUDED.fingerprint, reviewer_id = EXCLUDED.reviewer_id, updated_at = now()`,
		revision.ID, section, decision, strings.TrimSpace(comment), fingerprint, reviewerID)
	if err != nil {
		return ReviewWorkspace{}, fmt.Errorf("save rule review check: %w", err)
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, $2, 'site_rule.review_section', 'site_rule_revision', $3, $4)`,
		actor.Type, reviewerID, revision.ID, mustJSON(map[string]any{
			"section": section, "decision": decision, "comment": strings.TrimSpace(comment), "fingerprint": fingerprint,
		}))
	if err != nil {
		return ReviewWorkspace{}, fmt.Errorf("audit rule section review: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return ReviewWorkspace{}, fmt.Errorf("commit rule review: %w", err)
	}
	return s.reviewWorkspace(ctx, revision)
}

func (s *Store) reviewWorkspace(ctx context.Context, revision Revision) (ReviewWorkspace, error) {
	checks, err := s.loadReviewChecks(ctx, revision.ID)
	if err != nil {
		return ReviewWorkspace{}, err
	}
	sections, err := projectReviewSections(revision)
	if err != nil {
		return ReviewWorkspace{}, err
	}
	confirmed := 0
	blockers := make([]map[string]any, 0)
	sourceComplete := false
	policy, err := ParsePolicy(revision.Policy)
	if err != nil {
		return ReviewWorkspace{}, fmt.Errorf("decode rule source for review: %w", err)
	}
	sourceComplete = policy.Source.Complete
	for index := range sections {
		if check, ok := checks[sections[index].Key]; ok && check.Fingerprint == revision.Fingerprint {
			copy := check
			sections[index].Check = &copy
			if check.Decision == "confirmed" {
				confirmed++
				continue
			}
			blockers = append(blockers, map[string]any{"code": "rule_section_needs_changes", "section": sections[index].Key, "message": check.Comment})
			continue
		}
		blockers = append(blockers, map[string]any{"code": "rule_section_unreviewed", "section": sections[index].Key, "message": "该章节尚未绑定当前 fingerprint 完成人工确认"})
	}
	if revision.Status != "draft" {
		blockers = make([]map[string]any, 0)
	} else if !sourceComplete {
		blockers = append([]map[string]any{{"code": "rule_source_incomplete", "section": "source", "message": "source.complete=false；必须补充完整相关规则并导入新 revision"}}, blockers...)
	}
	if revision.Status == "draft" {
		blockers = append(hardGatePolicyBlockers(policy), blockers...)
	}
	return ReviewWorkspace{
		RevisionID: revision.ID, SiteCode: revision.SiteCode, Fingerprint: revision.Fingerprint,
		RevisionStatus: revision.Status, ApprovalReady: revision.Status == "draft" && sourceComplete && confirmed == len(sections) && len(hardGatePolicyBlockers(policy)) == 0,
		ConfirmedCount: confirmed, RequiredCount: len(sections), Sections: sections,
		Advisories: append([]Advisory{}, policy.Advisories...), Blockers: blockers,
		NextActions: []map[string]any{{"action": "review_rule_sections", "remaining": len(sections) - confirmed}},
	}, nil
}

func (s *Store) loadReviewChecks(ctx context.Context, revisionID string) (map[string]ReviewCheck, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT section, decision, COALESCE(comment, ''), fingerprint, reviewer_id::text, updated_at
		FROM site_rule_review_checks WHERE rule_revision_id = $1`, revisionID)
	if err != nil {
		return nil, fmt.Errorf("list rule review checks: %w", err)
	}
	defer rows.Close()
	checks := make(map[string]ReviewCheck)
	for rows.Next() {
		var check ReviewCheck
		if err := rows.Scan(&check.Section, &check.Decision, &check.Comment, &check.Fingerprint, &check.ReviewerID, &check.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan rule review check: %w", err)
		}
		checks[check.Section] = check
	}
	return checks, rows.Err()
}

func ensureReviewComplete(ctx context.Context, tx pgx.Tx, revision Revision) error {
	policy, err := ParsePolicy(revision.Policy)
	if err != nil {
		return fmt.Errorf("parse rule policy before approval: %w", err)
	}
	if blockers := hardGatePolicyBlockers(policy); len(blockers) > 0 {
		return fmt.Errorf("%w: unresolved executable hard-gate policy: %v", ErrReviewIncomplete, blockers[0]["code"])
	}
	var confirmed int
	err = tx.QueryRow(ctx, `
		SELECT count(*) FROM site_rule_review_checks
		WHERE rule_revision_id = $1 AND fingerprint = $2 AND decision = 'confirmed'
		  AND section = ANY($3)`, revision.ID, revision.Fingerprint, reviewSectionOrder).Scan(&confirmed)
	if err != nil {
		return fmt.Errorf("count rule review checks: %w", err)
	}
	if confirmed != len(reviewSectionOrder) {
		return fmt.Errorf("%w: %d of %d sections are confirmed for this fingerprint", ErrReviewIncomplete, confirmed, len(reviewSectionOrder))
	}
	return nil
}

func validReviewSection(value string) bool {
	for _, section := range reviewSectionOrder {
		if value == section {
			return true
		}
	}
	return false
}

func projectReviewSections(revision Revision) ([]ReviewSection, error) {
	typedPolicy, err := ParsePolicy(revision.Policy)
	if err != nil {
		return nil, err
	}
	var policy map[string]any
	if err := json.Unmarshal(revision.Policy, &policy); err != nil {
		return nil, fmt.Errorf("decode rule policy for review: %w", err)
	}
	get := func(key string) map[string]any {
		if value, ok := policy[key].(map[string]any); ok {
			return value
		}
		return map[string]any{}
	}
	limits := get("limits")
	sections := []ReviewSection{
		{Key: "upload_limit", Title: "上传限速硬门禁", Status: "extracted", Summary: "核对站点原值、20 MB/s 安全余量和单种执行上限", Data: map[string]any{
			"upload": limits["upload"], "seedbox_upload": limits["seedbox_upload"],
			"upload_policy": limits["upload_policy"], "seedbox_upload_policy": limits["seedbox_upload_policy"],
		}},
		{Key: "download_limit", Title: "下载限速硬门禁", Status: "extracted", Summary: "核对站点下载上限；默认按原值应用到单个种子", Data: map[string]any{
			"download": limits["download"], "download_policy": limits["download_policy"],
		}},
		{Key: "naming", Title: "强制命名硬门禁", Status: "extracted", Summary: "核对发布标题和内容根名称的强制格式", Data: get("naming")},
	}
	for index := range sections {
		sections[index].Facts = reviewFacts(sections[index].Key, typedPolicy, revision)
		if sections[index].Status == "not_extracted" && len(sections[index].Facts) > 0 && sections[index].Facts[0].Value != "未提取" {
			sections[index].Status = "partially_extracted"
		}
	}
	return sections, nil
}

func reviewFacts(section string, policy Policy, revision Revision) []ReviewFact {
	switch section {
	case "upload_limit":
		return []ReviewFact{
			ratePolicyFact("全局上传上限", policy.Limits.Upload, policy.Limits.UploadPolicy, "适用于该站点在所有下载器中的单个种子；设置和回读不一致会阻塞"),
			ratePolicyFact("盒子上传上限", policy.Limits.SeedboxUpload, policy.Limits.SeedboxUploadPolicy, "仅在下载器被人工标记为 SeedBox 时应用，并与全局上限取更严格值"),
		}
	case "download_limit":
		return []ReviewFact{ratePolicyFact("下载上限", policy.Limits.Download, policy.Limits.DownloadPolicy, "应用到该站点对应的单个种子；默认不扣减安全余量")}
	case "source":
		complete := "否，当前 revision 不能审批"
		tone := "danger"
		if policy.Source.Complete {
			complete, tone = "是，已声明为完整原文", "positive"
		}
		return []ReviewFact{
			fact("规则来源", policy.Source.URL, "打开来源页时仍需确认页面未更新", "neutral"),
			fact("采集时间", policy.Source.CapturedAt, "", "neutral"),
			fact("原文是否完整", complete, policy.Source.Scope, tone),
			fact("原文 SHA-256", policy.Source.TextSHA256, "用于证明审核时看到的原文未变化", "neutral"),
		}
	case "content":
		return obligationFacts(policy.Obligations, "内容范围", "content", "资源范围", "允许的资源")
	case "download":
		return []ReviewFact{
			fact("允许作为源站下载", allowedText(policy.Automation.Download), "这是规则文档中的能力声明，不代表账号当前拥有下载权限", boolTone(policy.Automation.Download)),
			fact("下载限速", limitText(policy.Limits.Download), "任务执行时应由下载器适配器应用并回读验证", limitTone(policy.Limits.Download)),
			fact("允许自动拉种", enabledText(policy.Automation.AutoPull), "关闭时必须停在人工确认边界", boolTone(policy.Automation.AutoPull)),
		}
	case "upload":
		return []ReviewFact{
			fact("允许作为目标站上传", allowedText(policy.Automation.Upload), "仍需账号资格、查重、规则接受与最终上传确认", boolTone(policy.Automation.Upload)),
			fact("上传限速", limitText(policy.Limits.Upload), "任务执行时应由下载器适配器应用并回读验证", limitTone(policy.Limits.Upload)),
			fact("允许自动上传", enabledText(policy.Automation.AutoUpload), "即使开启也不能绕过 confirm_upload", boolTone(policy.Automation.AutoUpload)),
		}
	case "retorrent":
		return []ReviewFact{
			fact("允许转种", allowedText(policy.Automation.Retorrent), "未知或禁转状态必须停止", boolTone(policy.Automation.Retorrent)),
			fact("必须保持文件内容与结构", requiredText(policy.Transfer.PreserveContent), "为“否”时表示结构化稿未设置此硬要求，不等于站规允许修改", boolTone(policy.Transfer.PreserveContent)),
			fact("禁止直接复用源站 torrent", requiredText(policy.Transfer.ForbidOriginalTorrent), "目标站 torrent 应重新生成并净化", boolTone(policy.Transfer.ForbidOriginalTorrent)),
		}
	case "promotions":
		return append([]ReviewFact{
			fact("必须为 Free 才能转种", requiredText(policy.Transfer.FreeleechRequired), "", boolTone(!policy.Transfer.FreeleechRequired)),
			fact("要求的促销状态", listText(policy.Transfer.RequiredPromotions), "空值表示结构化稿没有限定，不代表页面没有促销规则", "neutral"),
		}, obligationFacts(policy.Obligations, "相关义务", "promotion", "促销", "free")...)
	case "duplicates":
		return obligationFacts(policy.Obligations, "重复检查义务", "duplicate", "dupe", "查重", "重复")
	case "packaging":
		return obligationFacts(policy.Obligations, "打包与合集义务", "pack", "合集", "打包", "文件结构")
	case "naming":
		return namingFacts(policy.Naming)
	case "description":
		return obligationFacts(policy.Obligations, "描述与素材义务", "description", "描述", "截图", "mediainfo", "bdinfo")
	case "seeding":
		return []ReviewFact{
			fact("最短做种时间", hoursText(policy.Seeding.MinimumTimeHours), "实际种子若有更严格的 HR/H&R 要求，应采用更严格值", numberTone(policy.Seeding.MinimumTimeHours)),
			fact("最低分享率", ratioText(policy.Seeding.MinimumRatio), "账号和单种要求仍需分别核对", ratioTone(policy.Seeding.MinimumRatio)),
		}
	case "seedbox":
		return obligationFacts(policy.Obligations, "SeedBox / HR 义务", "seedbox", "seed box", "盒子", "hr", "h&r")
	case "limits":
		return []ReviewFact{
			fact("下载限速", limitText(policy.Limits.Download), "例如 20 MiB/s 表示每秒 20 MiB", limitTone(policy.Limits.Download)),
			fact("上传限速", limitText(policy.Limits.Upload), "任务级限速必须留下设置值和回读值证据", limitTone(policy.Limits.Upload)),
		}
	case "automation":
		return []ReviewFact{
			fact("必须人工审核规则", requiredText(policy.Automation.ManualReviewRequired), "", boolTone(policy.Automation.ManualReviewRequired)),
			fact("下载能力", allowedText(policy.Automation.Download), "", boolTone(policy.Automation.Download)),
			fact("上传能力", allowedText(policy.Automation.Upload), "", boolTone(policy.Automation.Upload)),
			fact("转种能力", allowedText(policy.Automation.Retorrent), "", boolTone(policy.Automation.Retorrent)),
			fact("自动拉种", enabledText(policy.Automation.AutoPull), "", boolTone(policy.Automation.AutoPull)),
			fact("自动上传", enabledText(policy.Automation.AutoUpload), "不能替代显式 confirm_upload", boolTone(policy.Automation.AutoUpload)),
		}
	case "access":
		if policy.SchemaVersion < 2 {
			return []ReviewFact{fact("网络访问授权", "未提取，默认禁止", "v1 规则不能授权服务访问站点；请导入并审核 site-rule v2。", "danger")}
		}
		return []ReviewFact{
			fact("服务访问", accessModeText(policy.Access.ServiceAccess), "forbidden 或 undetermined 都会在发送请求前阻塞", accessModeTone(policy.Access.ServiceAccess)),
			fact("搜索访问", accessModeText(policy.Access.SearchAccess), "候选扫描和目标站查重均按搜索计量", accessModeTone(policy.Access.SearchAccess)),
			fact("普通访问最小间隔", secondsText(policy.Access.GeneralMinIntervalSeconds), "与人工配置合并时采用更严格值", numberTone(policy.Access.GeneralMinIntervalSeconds)),
			fact("普通访问每小时配额", quotaText(policy.Access.GeneralMaxRequestsPerHour), "0 表示站规未给出数值，仍必须配置人工策略", numberTone(policy.Access.GeneralMaxRequestsPerHour)),
			fact("搜索最小间隔", secondsText(policy.Access.SearchMinIntervalSeconds), "与普通访问独立计量", numberTone(policy.Access.SearchMinIntervalSeconds)),
			fact("搜索每小时配额", quotaText(policy.Access.SearchMaxRequestsPerHour), "与普通访问独立计量", numberTone(policy.Access.SearchMaxRequestsPerHour)),
			fact("最大并发", concurrencyText(policy.Access.MaxConcurrency), "0 表示站规未给出数值，人工配置仍然必填", numberTone(policy.Access.MaxConcurrency)),
		}
	case "obligations":
		facts := make([]ReviewFact, 0, len(policy.Obligations))
		for _, obligation := range policy.Obligations {
			facts = append(facts, obligationFact(obligation, obligation.ID))
		}
		return facts
	case "original_text":
		return []ReviewFact{
			fact("Markdown SHA-256", revision.MarkdownSHA256, "读取时服务会再次校验文件内容", "neutral"),
			fact("规则来源", revision.SourceURL, "请在“原始规则全文”标签页逐条核对", "neutral"),
		}
	default:
		return nil
	}
}

func hardGatePolicyBlockers(policy Policy) []map[string]any {
	result := make([]map[string]any, 0, len(policy.Source.Conflicts)+3)
	for _, conflict := range policy.Source.Conflicts {
		result = append(result, map[string]any{
			"code": "rule_source_conflict", "section": conflict.Section,
			"message": conflict.Summary, "evidence_refs": conflict.EvidenceRefs,
		})
	}
	for section, rate := range map[string]*RateLimitPolicy{
		"download_limit":       policy.Limits.DownloadPolicy,
		"upload_limit":         policy.Limits.UploadPolicy,
		"seedbox_upload_limit": policy.Limits.SeedboxUploadPolicy,
	} {
		if rate == nil || strings.TrimSpace(rate.Declared) == "" {
			continue
		}
		if rate.Scope != "per_torrent" {
			result = append(result, map[string]any{"code": "rule_limit_scope_unresolved", "section": section, "message": "原文限速不是明确的单种范围；必须人工给出可执行的单种上限"})
		} else if strings.TrimSpace(rate.Enforced) == "" {
			result = append(result, map[string]any{"code": "rule_limit_value_unresolved", "section": section, "message": "站点原值不高于默认余量或执行值缺失；必须人工填写最终单种限速"})
		}
	}
	return result
}

func ratePolicyFact(label, fallback string, policy *RateLimitPolicy, detail string) ReviewFact {
	if policy == nil {
		return fact(label, limitText(fallback), detail, limitTone(fallback))
	}
	value := "原文 " + limitText(policy.Declared)
	if strings.TrimSpace(policy.Enforced) != "" {
		value += " → 执行 " + limitText(policy.Enforced)
	} else if strings.TrimSpace(policy.Declared) != "" {
		value += " → 待人工设置"
	}
	if strings.TrimSpace(policy.SafetyMargin) != "" {
		detail = "安全余量 " + limitText(policy.SafetyMargin) + "；" + detail
	}
	if policy.Scope != "per_torrent" {
		detail = "原文范围 " + policy.Scope + "；" + detail
	}
	return fact(label, value, detail, limitTone(policy.Enforced))
}

func namingFact(label string, constraint NamingConstraint) ReviewFact {
	if !constraint.Required {
		if strings.TrimSpace(constraint.Template) != "" {
			detail := "AI 已提取格式说明，但没有生成可执行的强制校验表达式"
			if strings.TrimSpace(constraint.Pattern) != "" && strings.TrimSpace(constraint.Pattern) != "^.*$" {
				detail += "；候选表达式：" + constraint.Pattern
			}
			return fact(label, "已提取但当前不可执行："+constraint.Template, detail, "warning")
		}
		return fact(label, "原文未提取强制格式", "确认前应核对原文确实没有强制命名要求", "warning")
	}
	value := constraint.Template
	if strings.TrimSpace(value) == "" {
		value = constraint.Pattern
	}
	detail := "校验表达式：" + constraint.Pattern
	if constraint.MaxLength > 0 {
		detail += fmt.Sprintf("；最长 %d 字符", constraint.MaxLength)
	}
	return fact(label, value, detail, "positive")
}

func namingFacts(naming Naming) []ReviewFact {
	facts := make([]ReviewFact, 0, len(naming.Profiles)+2)
	if len(naming.Profiles) == 0 {
		facts = append(facts, namingFact("发布标题", naming.ReleaseTitle))
	} else {
		if naming.ReleaseTitle.Required || strings.TrimSpace(naming.ReleaseTitle.Template) != "" {
			facts = append(facts, namingFact("发布标题 · 通用规则", naming.ReleaseTitle))
		}
		for _, profile := range naming.Profiles {
			facts = append(facts, namingFact("发布标题 · "+profile.Label, profile.ReleaseTitle))
		}
	}
	facts = append(facts, namingFact("内容根名称", naming.ContentName))
	return facts
}

func accessReviewStatus(policy Policy) string {
	if policy.SchemaVersion < 2 {
		return "not_extracted"
	}
	return "extracted"
}

func accessModeText(value string) string {
	return map[string]string{"allowed": "允许", "forbidden": "禁止", "undetermined": "未确定"}[value]
}

func accessModeTone(value string) string {
	if value == "allowed" {
		return "positive"
	}
	return "danger"
}

func secondsText(value int) string {
	if value <= 0 {
		return "站规未给出数值"
	}
	return fmt.Sprintf("%d 秒", value)
}

func quotaText(value int) string {
	if value <= 0 {
		return "站规未给出数值"
	}
	return fmt.Sprintf("每小时 %d 次", value)
}

func concurrencyText(value int) string {
	if value <= 0 {
		return "站规未给出数值"
	}
	return fmt.Sprintf("%d", value)
}

func obligationFacts(obligations []Obligation, label string, keywords ...string) []ReviewFact {
	result := make([]ReviewFact, 0)
	for _, obligation := range obligations {
		haystack := strings.ToLower(strings.Join([]string{obligation.ID, obligation.Scope, obligation.Description, obligation.Enforcement}, " "))
		for _, keyword := range keywords {
			if strings.Contains(haystack, strings.ToLower(keyword)) {
				result = append(result, obligationFact(obligation, label))
				break
			}
		}
	}
	if len(result) == 0 {
		return []ReviewFact{fact("结构化状态", "未提取", "当前 v1 文档没有该章节的独立字段；请对照原始规则全文审核，不要据此推断为无限制。", "warning")}
	}
	return result
}

func obligationFact(obligation Obligation, label string) ReviewFact {
	state := "非阻塞"
	tone := "neutral"
	if obligation.Blocking {
		state, tone = "阻塞义务", "danger"
	}
	verification := "程序验证"
	if obligation.Verification == "manual" {
		verification = "人工验证"
	}
	resolution := map[string]string{"pending": "待解决", "enforced": "已由门禁执行", "not_applicable": "不适用"}[obligation.Resolution]
	detail := strings.TrimSpace(obligation.Enforcement)
	if len(obligation.EvidenceRefs) > 0 {
		detail += "；依据：" + strings.Join(obligation.EvidenceRefs, "；")
	}
	return fact(label, obligation.Description, state+" · "+verification+" · "+resolution+"。"+detail, tone)
}

func fact(label, value, detail, tone string) ReviewFact {
	return ReviewFact{Label: label, Value: value, Detail: strings.TrimSpace(detail), Tone: tone}
}

func allowedText(value bool) string {
	if value {
		return "允许"
	}
	return "不允许"
}
func enabledText(value bool) string {
	if value {
		return "开启"
	}
	return "关闭"
}
func requiredText(value bool) string {
	if value {
		return "是"
	}
	return "否"
}
func boolTone(value bool) string {
	if value {
		return "positive"
	}
	return "warning"
}
func limitTone(value string) string {
	if strings.TrimSpace(value) == "" {
		return "warning"
	}
	return "positive"
}
func numberTone(value int) string {
	if value == 0 {
		return "warning"
	}
	return "positive"
}
func ratioTone(value float64) string {
	if value == 0 {
		return "warning"
	}
	return "positive"
}
func limitText(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "未声明"
	}
	return strings.NewReplacer("MiB/s", " MiB/s", "MB/s", " MB/s", "KiB/s", " KiB/s").Replace(value)
}
func listText(values []string) string {
	if len(values) == 0 {
		return "未声明"
	}
	return strings.Join(values, "、")
}
func hoursText(value int) string {
	if value == 0 {
		return "未声明"
	}
	if value%24 == 0 {
		return fmt.Sprintf("%d 小时（%d 天）", value, value/24)
	}
	return fmt.Sprintf("%d 小时", value)
}
func ratioText(value float64) string {
	if value == 0 {
		return "未声明"
	}
	return fmt.Sprintf("%.2f", value)
}
