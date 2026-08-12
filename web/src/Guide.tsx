export type GuideDestination = "jobs" | "candidates" | "configuration" | "readiness" | "audit";
export type GuideTab = "operator" | "agent";

interface GuideProps {
  tab: GuideTab;
  onTabChange: (tab: GuideTab) => void;
  onNavigate: (destination: GuideDestination) => void;
  onCreateJob: () => void;
}

const workflow = [
  "识别源站", "审核规则", "拉取并完成下载", "生成媒体资料", "目标站查重", "人工确认上传", "注入并核验做种",
];

const agentEndpoints = [
  {path: "/.well-known/upload-assistant.json", title: "服务清单", copy: "Agent 首先读取的能力入口。"},
  {path: "/.well-known/upload-assistant/SKILL.md", title: "Skill 说明", copy: "包含安全边界和推荐调用顺序。"},
  {path: "/openapi.json", title: "OpenAPI", copy: "稳定的 HTTP 请求与响应结构。"},
  {path: "/api/v2/tools", title: "工具目录", copy: "列出工具、权限和安全等级。"},
];

export default function Guide({tab, onTabChange, onNavigate, onCreateJob}: GuideProps) {
  return (
    <main className="guide-pane">
      <header className="guide-header">
        <p>{tab === "operator" ? "按实际操作顺序完成配置和第一条转种任务。" : "让 OpenClaw、Hermes 或其他 Agent 安全调用本地服务。"}</p>
        <nav className="help-tabs" aria-label="帮助主题">
          <button className={tab === "operator" ? "active" : ""} onClick={() => onTabChange("operator")}>人工使用</button>
          <button className={tab === "agent" ? "active" : ""} onClick={() => onTabChange("agent")}>Agent 接入</button>
        </nav>
      </header>

      {tab === "operator" ? <OperatorGuide onNavigate={onNavigate} onCreateJob={onCreateJob} /> : <AgentGuide onNavigate={onNavigate} />}
    </main>
  );
}

function OperatorGuide({onNavigate, onCreateJob}: Pick<GuideProps, "onNavigate" | "onCreateJob">) {
  return <>
    <section className="guide-steps" aria-label="首次使用步骤">
      <GuideStep number="1" title="配置站点与规则" action="打开站点规则" onAction={() => onNavigate("configuration")}>
        保存站点 Cookie 与访问频率，受控采集多个规则页，只核对单种上传、单种下载与分类命名后批准并激活。
      </GuideStep>
      <GuideStep number="2" title="接入工具链" action="配置下载器等集成" onAction={() => onNavigate("configuration")}>
        添加下载器与路径映射，人工标记家宽或盒子，再配置图床、截图和元数据服务。
      </GuideStep>
      <GuideStep number="3" title="检查本地环境" action="运行环境检查" onAction={() => onNavigate("readiness")}>
        核对规则、凭据字段、挂载路径和本地工具，不会连接外部服务。
      </GuideStep>
      <GuideStep number="4" title="创建逐步任务" action="创建转种任务" onAction={onCreateJob}>
        输入合法的源站详情链接；任务会在规则、查重和上传确认处停下。
      </GuideStep>
    </section>

    <details className="guide-disclosure">
      <summary><span>查看完整任务流程</span><small>7 个阶段，每一步都可审计</small></summary>
      <div className="workflow-map">
        {workflow.map((item, index) => <div key={item}><span>{index + 1}</span><strong>{item}</strong></div>)}
      </div>
    </details>

    <section className="guide-compact-notes">
      <article><h2>开始前准备</h2><ul><li>合法站点账号和允许测试的详情链接</li><li>盒子下载目录到容器 <code>/downloads</code> 的路径映射</li><li>当前站规、下载器、图床及元数据配置</li></ul></article>
      <article><h2>不会被跳过的门禁</h2><p>规则指纹、目标查重、人工义务、上传包、规则接受、上传确认和做种要求都必须有明确证据。</p><button className="secondary" onClick={() => onNavigate("audit")}>查看审计记录</button></article>
    </section>
  </>;
}

function AgentGuide({onNavigate}: Pick<GuideProps, "onNavigate">) {
  return <>
    <section className="agent-intro">
      <div><h2>推荐接入方式</h2><p>把服务地址和 API Token 作为 Agent 的受控配置。Agent 先读取能力描述，再根据响应中的状态和下一步操作推进任务。</p></div>
      <button className="secondary" onClick={() => onNavigate("readiness")}>先检查本地环境</button>
    </section>

    <section className="agent-endpoints" aria-label="Agent 发现入口">
      {agentEndpoints.map((endpoint) => <article key={endpoint.path}><div><strong>{endpoint.title}</strong><p>{endpoint.copy}</p></div><code>{endpoint.path}</code></article>)}
    </section>

    <section className="agent-guide-grid">
      <article>
        <h2>1. 配置认证</h2>
        <p>由本机管理员签发 Token，并通过密钥管理或环境变量交给 Agent。不要把 Token 写进提示词、URL 或聊天记录。</p>
        <pre><code>docker compose exec upload-assistant upload-assistant admin token issue \
  --username &lt;管理员名&gt; --name agent --confirm</code></pre>
      </article>
      <article>
        <h2>2. 发现工具</h2>
        <p>支持 OpenAPI 的 Agent 使用 <code>/openapi.json</code>；支持 Skill 的 Agent 使用 well-known 地址。工具目录可用于能力和权限预检。</p>
        <pre><code>Authorization: Bearer ua_…
GET /api/v2/tools</code></pre>
      </article>
      <article>
        <h2>3. 按任务状态循环</h2>
        <p>创建任务后保存 <code>job_id</code>，轮询任务状态；优先读取下面这些短路径字段，不要从日志文本猜测状态。</p>
        <div className="agent-fields"><code>status</code><code>ok</code><code>blockers</code><code>next_actions</code><code>resume_state</code><code>summary</code></div>
      </article>
      <article>
        <h2>4. 保留人工边界</h2>
        <p><code>blocked</code> 是需要处理的稳定状态，不是失败。Agent 不得代替用户接受新规则、伪造人工义务证据或默认确认 live 上传。</p>
        <div className="agent-warning"><strong>上传前必须显式提供</strong><code>accept_rules + confirm_upload=true</code></div>
      </article>
    </section>

    <details className="guide-disclosure agent-sequence">
      <summary><span>查看推荐调用顺序</span><small>发现 → 创建 → 查询 → 处理阻塞 → 获取总结</small></summary>
      <ol><li>读取服务清单、Skill 或 OpenAPI。</li><li>读取工具目录并确认 Token scope。</li><li>创建任务并持久化返回的 <code>job_id</code>。</li><li>查询状态；遇到 <code>blocked</code> 时展示 blockers 和 next_actions。</li><li>只提交用户明确提供的恢复参数，随后继续查询。</li><li>任务完成后读取 summary 和证据引用。</li></ol>
    </details>
  </>;
}

function GuideStep({number, title, action, onAction, children}: {
  number: string;
  title: string;
  action: string;
  onAction: () => void;
  children: string;
}) {
  return (
    <article>
      <span className="guide-step-number">{number}</span>
      <div><h2>{title}</h2><p>{children}</p></div>
      <button onClick={onAction}>{action}<span aria-hidden="true">→</span></button>
    </article>
  );
}
