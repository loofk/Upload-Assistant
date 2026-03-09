const { useState, useRef, useEffect, useCallback } = React;
const THEME_KEY = 'ua_config_theme';

const storage = window.UAStorage;
const getStoredTheme = window.getUAStoredTheme;

// Local CSRF cache used by fallback `apiFetch` when `uaApiFetch` isn't present.
let localCsrf = null;
const loadLocalCsrf = async (force = false) => {
  if (localCsrf && !force) return;

  let apiBase = '';
  if (typeof window !== 'undefined' && window.location) {
    apiBase = window.location.origin + '/api';
  } else {
    apiBase = '/api';
  }

  try {
    const r = await fetch(`${apiBase}/csrf_token`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    localCsrf = d && d.csrf_token ? String(d.csrf_token) : null;
  } catch (e) {
    // ignore
  }
};

// Prefer shared `uaApiFetch` when available (provides CSRF handling and retry-on-auth-fail),
// otherwise fall back to a local implementation.
const apiFetch = (typeof window !== 'undefined' && window.uaApiFetch) || (async (url, options = {}) => {
  // Local fallback: load CSRF token once and retry on 401/403 once.
  await loadLocalCsrf();
  const headers = { ...(options.headers || {}) };
  if (localCsrf) headers['X-CSRF-Token'] = localCsrf;
  let response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
  if (response.status === 401 || response.status === 403) {
    await loadLocalCsrf(true);
    const headers2 = { ...(options.headers || {}) };
    if (localCsrf) headers2['X-CSRF-Token'] = localCsrf;
    response = await fetch(url, { ...options, headers: headers2, credentials: 'same-origin' });
  }
  return response;
});

const sanitizeHtml = window.sanitizeHtml;

// Argument categories for the right sidebar (placeholders shown for info only)
const argumentCategories = [
  {
    title: "模式 / 工作流",
    args: [
      { label: "--queue", placeholder: "QUEUE_NAME", description: "按队列名处理整个目录" },
      { label: "--limit-queue", placeholder: "N", description: "限制队列成功上传数量" },
      { label: "--site-check", description: "仅站点检查（能否上传，不发种）" },
      { label: "--site-upload", placeholder: "TRACKER", description: "按单站点处理 site-check 结果并上传" },
      { label: "--search_requests", description: "在支持的站点中搜索匹配求种（需配置）" },
      { label: "--unit3d", description: "从 UNIT3D-Upload-Checker 结果上传" }
    ]
  },
  {
    title: "元数据 / 各类 ID",
    subtitle: "这些填对了，上传就成功了 90%！",
    args: [
      { label: "--tmdb", placeholder: "movie/123", description: "TMDb ID" },
      { label: "--imdb", placeholder: "tt0111161", description: "IMDb ID" },
      { label: "--mal", placeholder: "ID", description: "MAL ID（常用于番剧）" },
      { label: "--tvmaze", placeholder: "ID", description: "TVMaze ID" },
      { label: "--tvdb", placeholder: "ID", description: "TVDB ID" }
    ]
  },
  {
    title: "截图 / 图片",
    args: [
      { label: "--screens", placeholder: "N", description: "生成的截图数量" },
      { label: "--manual_frames", placeholder: '"1,250,500"', description: "手动指定截图帧号" },
      { label: "--comparison", placeholder: "PATH", description: "对比图所在文件夹" },
      { label: "--comparison_index", placeholder: "N", description: "主对比图下标" },
      { label: "--disc-menus", placeholder: "PATH", description: "光盘菜单截图文件夹（BD/DVD）" },
      { label: "--imghost", placeholder: "HOST", description: "选择使用的图床" },
      { label: "--skip-imagehost-upload", description: "跳过上传截图到图床" }
    ]
  },
  {
    title: "剧集相关参数",
    args: [
      { label: "--season", placeholder: "S01", description: "季号" },
      { label: "--episode", placeholder: "E01", description: "集号" },
      { label: "--manual-episode-title", placeholder: "TITLE", description: "手动指定单集标题" },
      { label: "--daily", placeholder: "YYYY-MM-DD", description: "日播节目播出日期" }
    ]
  },
  {
    title: "标题格式调整",
    args: [
      { label: "--year", placeholder: "YYYY", description: "覆盖年份" },
      { label: "--no-season", description: "标题中移除季信息" },
      { label: "--no-year", description: "标题中移除年份" },
      { label: "--no-aka", description: "移除 AKA 信息" },
      { label: "--no-dub", description: "移除“DUBBED”标记" },
      { label: "--no-dual", description: "移除“双语音轨”标记" },
      { label: "--no-tag", description: "移除小组标签" },
      { label: "--no-edition", description: "移除版本/加长版等标记" },
      { label: "--dual-audio", description: "添加双语音轨标记" },
      { label: "--tag", placeholder: "GROUP", description: "小组名标签" },
      { label: "--service", placeholder: "SERVICE", description: "流媒体服务名" },
      { label: "--region", placeholder: "REGION", description: "光盘地区码" },
      { label: "--edition", placeholder: "TEXT", description: "版本标记" },
      { label: "--repack", placeholder: "TEXT", description: "Repack 标记" }
    ]
  },
  {
    title: "简介 / NFO",
    args: [
      { label: "--desclink", placeholder: "URL", description: "外部描述链接（Pastebin/Hastebin 等）" },
      { label: "--descfile", placeholder: "PATH", description: "本地描述文件路径（.txt/.nfo/.md）" },
      { label: "--nfo", description: "使用目录中的 .nfo 作为描述" }
    ]
  },
  {
    title: "语言设置",
    args: [
      { label: "--original-language", placeholder: "en", description: "原始音轨语言" },
      { label: "--only-if-languages", placeholder: "en,fr", description: "仅当文件包含这些语言时才继续上传" }
    ]
  },
  {
    title: "其他元数据开关",
    args: [
      { label: "--commentary", description: "包含评论音轨" },
      { label: "--sfx-subtitles", description: "包含 SFX 字幕" },
      { label: "--extras", description: "包含额外花絮/特典" },
      { label: "--distributor", placeholder: "NAME", description: "发行公司（Criterion、BFI 等）" },
      { label: "--sorted-filelist", description: "按排序后的文件列表选主视频（常用于番剧目录）" },
      { label: "--keep-folder", description: "单文件上传时保留上级目录" },
      { label: "--keep-nfo", description: "保留 NFO 文件（极少数站点用）" },
    ]
  },
  {
    title: "源站引用",
    subtitle: "从这些站点拉取 ID / 描述 / 截图等信息",
    args: [
      { label: "--onlyID", description: "只抓取元数据 ID，不使用站点描述" },
      { label: "--ptp", placeholder: "ID_OR_URL", description: "PTP ID/链接" },
      { label: "--blu", placeholder: "ID_OR_URL", description: "BLU ID/链接" },
      { label: "--aither", placeholder: "ID_OR_URL", description: "Aither ID/链接" },
      { label: "--lst", placeholder: "ID_OR_URL", description: "LST ID/链接" },
      { label: "--oe", placeholder: "ID_OR_URL", description: "OE ID/链接" },
      { label: "--hdb", placeholder: "ID_OR_URL", description: "HDB ID/链接" },
      { label: "--btn", placeholder: "ID_OR_URL", description: "BTN ID/链接" },
      { label: "--bhd", placeholder: "ID_OR_URL", description: "BHD ID/链接" },
      { label: "--huno", placeholder: "ID_OR_URL", description: "HUNO ID/链接" },
      { label: "--ulcx", placeholder: "ID_OR_URL", description: "ULCX ID/链接" },
      { label: "--torrenthash", placeholder: "HASH", description: "仅在 qBittorrent 中：从 torrent 注释里解析站点 ID" }
    ]
  },
  {
    title: "上传选择 / 查重相关",
    args: [
      { label: "--trackers", placeholder: "aither,lst,ptp,etc", description: "覆盖默认 trackers，仅向这些站点上传" },
      { label: "--trackers-remove", placeholder: "blu,xyz,etc", description: "从默认 trackers 中移除这些站点" },
      { label: "--trackers-pass", placeholder: "N", description: "至少多少个站点检查通过才继续上传" },
      { label: "--skip_auto_torrent", description: "跳过自动从客户端搜索种子" },
      { label: "--skip-dupe-check", description: "跳过查重（需非常确定自己在做什么）" },
      { label: "--skip-dupe-asking", description: "查到重复时不再询问，直接按重复处理" },
      { label: "--double-dupe-check", description: "在上传前再跑一遍查重（适合抢首发）" },
      { label: "--draft", description: "发送到草稿（支持的站点）" },
      { label: "--modq", description: "发送到 modQ（支持的站点）" },
      { label: "--freeleech", placeholder: "25%", description: "设为 Freeleech（百分比）" }
    ]
  },
  {
    title: "匿名 / 做种 / 流媒体",
    args: [
      { label: "--anon", description: "匿名发布（支持的站点）" },
      { label: "--no-seed", description: "不把种子添加到客户端做种" },
      { label: "--stream", description: "流媒体优化" },
      { label: "--webdv", description: "Dolby Vision 混合（HYBRID）" },
      { label: "--hardcoded-subs", description: "包含硬字幕" },
      { label: "--personalrelease", description: "个人发布" }
    ]
  },
  {
    title: "种子创建 / 哈希",
    args: [
      { label: "--max-piece-size", placeholder: "N", description: "创建种子的最大分片大小（MiB，1–128）" },
      { label: "--nohash", description: "即便需要也不重新计算种子哈希" },
      { label: "--rehash", description: "从实际数据重新创建种子，而不是复用现有 .torrent" },
      { label: "--mkbrr", description: "使用 mkbrr 创建种子（需配置）" },
      { label: "--entropy", placeholder: "N", description: "熵（随机性）设置" },
      { label: "--randomized", placeholder: "N", description: "额外生成 N 个随机 infohash 的种子" },
      { label: "--infohash", placeholder: "HASH", description: "使用指定 infohash 作为已有种子的基准" },
      { label: "--force-recheck", description: "仅 qBittorrent：上传前在客户端强制重新校验文件" }
    ]
  },
  {
    title: "下载客户端集成",
    args: [
      { label: "--client", placeholder: "NAME", description: "使用指定配置中的客户端名称" },
      { label: "--qbit-tag", placeholder: "TAG", description: "添加到 qBittorrent 的标签" },
      { label: "--qbit-cat", placeholder: "CATEGORY", description: "添加到 qBittorrent 的分类" },
      { label: "--rtorrent-label", placeholder: "LABEL", description: "rTorrent 标签" }
    ]
  },
  {
    title: "临时文件 / 清理",
    args: [
      { label: "--delete-tmp", description: "删除本次上传对应的 tmp 目录" },
      { label: "--cleanup", description: "清理整个 UA 的 tmp 目录" }
    ]
  },
  {
    title: "调试 / 输出",
    args: [
      { label: "--debug", description: "调试模式（不实际上传）" },
      { label: "--ffdebug", description: "FFmpeg 调试输出" },
      { label: "--upload-timer", description: "打印各站上传耗时（需配置）" }
    ]
  },
  {
    title: "其他选项",
    args: [
      { label: "--not-anime", description: "标记为非动画，可加快某些 TV 数据抓取" },
      { label: "--channel", placeholder: "ID_OR_TAG", description: "SPD 频道 ID 或标签" },
      { label: "--unattended", description: "无人值守模式（完全无交互）" },
      { label: "--unattended_confirm", description: "无人值守但保留少量关键确认（需配合 --unattended 使用）" }
    ]
  }
];

// Icon components
const FolderIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
  </svg>
);

const FolderOpenIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
  </svg>
);

const FileIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
  </svg>
);

const TerminalIcon = () => (
  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
  </svg>
);

const PlayIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
  </svg>
);

const TrashIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
  </svg>
);

const UploadIcon = () => (
  <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
  </svg>
);

const ChevronDownIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
  </svg>
);

const ChevronRightIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
  </svg>
);

const SearchIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
  </svg>
);

const CollapseAllIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
  </svg>
);

const ExpandAllIcon = () => (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
  </svg>
);

const SpinnerIcon = () => (
  <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
  </svg>
);

function AudionutsUAGUI() {
  const API_BASE = window.location.origin + '/api';
  // Derive an application base path from the API base so links work under subpath deployments
  const APP_BASE = API_BASE.replace(/\/api$/, '');
  
  const [directories, setDirectories] = useState([
    { name: 'data', type: 'folder', path: '/data', children: [] },
    { name: 'torrent_storage_dir', type: 'folder', path: '/torrent_storage_dir', children: [] },
    { name: 'Upload-Assistant', type: 'folder', path: '/Upload-Assistant', children: [] }
  ]);
  
  const [selectedPath, setSelectedPath] = useState('');
  const [, setSelectedName] = useState('');
  const [customArgs, setCustomArgs] = useState('');
  const [isExecuting, setIsExecuting] = useState(false);
  const [expandedFolders, setExpandedFolders] = useState(new Set(['/data', '/torrent_storage_dir']));
  const [sessionId, setSessionId] = useState('');
  const [sidebarWidth, setSidebarWidth] = useState(320);
  const [isResizing, setIsResizing] = useState(false);
  const [rightSidebarWidth, setRightSidebarWidth] = useState(320);
  const [isResizingRight, setIsResizingRight] = useState(false);
  const [userInput, setUserInput] = useState('');
  const [isDarkMode, setIsDarkMode] = useState(getStoredTheme);
  const [argSearchFilter, setArgSearchFilter] = useState('');
  const [collapsedSections, setCollapsedSections] = useState(new Set());
  
  // Mobile state
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);
  const [activePanel, setActivePanel] = useState('main'); // 'main' | 'files' | 'args'
  
  // File Browser search states
  const [fileBrowserSearch, setFileBrowserSearch] = useState('');
  const [fileBrowserSearchResults, setFileBrowserSearchResults] = useState(null);
  const [fileBrowserSearchLoading, setFileBrowserSearchLoading] = useState(false);
  const fileBrowserSearchTimer = useRef(null);
  const fileBrowserSearchQuery = useRef('');

  // Folder loading states
  const [loadingFolders, setLoadingFolders] = useState(new Set());
  
  // Description file/link states
  const [descDirectories, setDescDirectories] = useState([]);
  const [descExpandedFolders, setDescExpandedFolders] = useState(new Set());
  const [descLoadingFolders, setDescLoadingFolders] = useState(new Set());
  const [descLinkError, setDescLinkError] = useState('');
  const [descFileError, setDescFileError] = useState('');
  const [descBrowserCollapsed, setDescBrowserCollapsed] = useState(false);
  const [descLinkFocused, setDescLinkFocused] = useState(false);
  
  const richOutputRef = useRef(null);
  const lastFullHashRef = useRef('');
  const inputRef = useRef(null);
  const sseAbortControllerRef = useRef(null);
  
  // Detect if --descfile or --desclink is present in arguments
  const hasDescFile = customArgs.includes('--descfile');
  const hasDescLink = customArgs.includes('--desclink');
  
  // URL validation helper - accepts any HTTP/HTTPS URL (server fetches and parses any URL)
  const isValidUrl = (string) => {
    try {
      const url = new URL(string);
      return url.protocol === 'http:' || url.protocol === 'https:';
    } catch (_) {
      return false;
    }
  };
  
  // Path validation helper - checks if string looks like a valid file path
  const isValidDescFilePath = (path) => {
    if (!path || path.trim() === '') return { valid: false, error: '' };
    
    const trimmed = path.trim();
    
    // Check for valid description file extensions
    const validExtensions = ['.txt', '.nfo', '.md'];
    const hasValidExt = validExtensions.some(ext => trimmed.toLowerCase().endsWith(ext));
    
    // Check if it looks like a path (has separators or starts with drive letter/root)
    const hasPathSeparator = trimmed.includes('/') || trimmed.includes('\\');
    const startsWithRoot = /^[a-zA-Z]:/.test(trimmed) || trimmed.startsWith('/') || trimmed.startsWith('\\');
    const looksLikePath = hasPathSeparator || startsWithRoot;
    
    if (!looksLikePath) {
      return { 
        valid: false, 
        error: 'Path should be a full file path (e.g., /path/to/desc.txt or C:\\path\\desc.txt)' 
      };
    }
    
    if (!hasValidExt) {
      return { 
        valid: false, 
        error: 'File should have a valid extension (.txt, .nfo, or .md)' 
      };
    }
    
    return { valid: true, error: '' };
  };
  
  // Extract value from argument string (e.g., --descfile "path" or --desclink "url")
  // Supports both space-separated (--arg "value") and equals-separated (--arg="value") formats
  const extractArgValue = (args, argName) => {
    // First try equals-separated format: --argname="value" or --argname='value' or --argname=value
    const equalsRegex = new RegExp(`${argName}=(?:"([^"]+)"|'([^']+)'|([^\\s]+))`, 'i');
    const equalsMatch = args.match(equalsRegex);
    if (equalsMatch) {
      const val = equalsMatch[1] || equalsMatch[2] || equalsMatch[3] || '';
      // Double-check: don't return values that look like arguments
      if (val.startsWith('--')) return '';
      return val.trim();
    }
    
    // Then try space-separated format: --argname "value" or --argname 'value' or --argname value
    const spaceRegex = new RegExp(`${argName}\\s+(?:"([^"]+)"|'([^']+)'|([^\\s-][^\\s]*|(?!--)[^\\s]+))`, 'i');
    const spaceMatch = args.match(spaceRegex);
    if (spaceMatch) {
      const val = spaceMatch[1] || spaceMatch[2] || spaceMatch[3] || '';
      // Double-check: don't return values that look like arguments
      if (val.startsWith('--')) return '';
      return val.trim();
    }
    return '';
  };
  
  // Update argument value in string
  // Supports both space-separated (--arg "value") and equals-separated (--arg="value") formats
  const updateArgValue = (args, argName, value) => {
    // Check if argument exists
    if (!args.includes(argName)) {
      return args;
    }
    
    // Check which format is being used
    const hasEqualsFormat = new RegExp(`${argName}=`, 'i').test(args);
    const hasSpaceValue = new RegExp(`${argName}\\s+(?:"[^"]*"|'[^']*'|(?!--)[^\\s]+)`, 'i').test(args);
    
    // If value is empty, remove the value but keep the flag
    if (!value) {
      if (hasEqualsFormat) {
        // Remove equals-format value: --arg="value" or --arg='value' or --arg=value
        return args.replace(new RegExp(`(${argName})=(?:"[^"]*"|'[^']*'|[^\\s]*)`, 'i'), '$1');
      } else if (hasSpaceValue) {
        // Remove space-format value
        return args.replace(new RegExp(`(${argName})\\s+(?:"[^"]*"|'[^']*'|(?!--)[^\\s]+)`, 'i'), '$1');
      }
      return args;
    }
    
    // Quote the value if it contains spaces
    const quotedValue = value.includes(' ') ? `"${value}"` : `"${value}"`;
    
    if (hasEqualsFormat) {
      // Replace equals-format value: --arg="value" or --arg='value' or --arg=value
      return args.replace(new RegExp(`(${argName})=(?:"[^"]*"|'[^']*'|[^\\s]*)`, 'i'), `$1=${quotedValue}`);
    } else if (hasSpaceValue) {
      // Replace space-format value
      return args.replace(new RegExp(`(${argName})\\s+(?:"[^"]*"|'[^']*'|(?!--)[^\\s]+)`, 'i'), `$1 ${quotedValue}`);
    } else {
      // Add value after the flag (no existing value)
      return args.replace(new RegExp(`(${argName})(\\s|$)`, 'i'), `$1 ${quotedValue}$2`);
    }
  };
  
  // Get current values from args
  const descFilePath = extractArgValue(customArgs, '--descfile');
  const descLinkUrl = extractArgValue(customArgs, '--desclink');
  
  // Validate desclink URL when it changes
  useEffect(() => {
    if (hasDescLink && descLinkUrl) {
      if (!isValidUrl(descLinkUrl)) {
        setDescLinkError('Please enter a valid paste URL (pastebin, hastebin, etc.)');
      } else {
        setDescLinkError('');
      }
    } else {
      setDescLinkError('');
    }
  }, [descLinkUrl, hasDescLink]);
  
  // Validate descfile path when it changes and auto-collapse when valid
  useEffect(() => {
    if (hasDescFile && descFilePath) {
      const validation = isValidDescFilePath(descFilePath);
      setDescFileError(validation.error);
      // Auto-collapse when valid file is selected
      if (validation.valid) {
        setDescBrowserCollapsed(true);
      }
    } else {
      setDescFileError('');
    }
  }, [descFilePath, hasDescFile]);
  
  // Reset description browser when argument is removed
  useEffect(() => {
    if (!hasDescFile) {
      setDescDirectories([]);
      setDescExpandedFolders(new Set());
      setDescFileError('');
      setDescBrowserCollapsed(false);
    }
    if (!hasDescLink) {
      setDescLinkError('');
    }
  }, [hasDescFile, hasDescLink]);
  
  // Update descfile in args
  const updateDescFile = (path) => {
    setCustomArgs(prev => updateArgValue(prev, '--descfile', path));
  };
  
  // Update desclink in args
  const updateDescLink = (url) => {
    setCustomArgs(prev => updateArgValue(prev, '--desclink', url));
  };

  const appendHtmlFragment = (rawHtml) => {
    const container = richOutputRef.current;
    if (container) {
      const clean = sanitizeHtml((rawHtml || '').trim());
      const wrapper = document.createElement('div');
      wrapper.innerHTML = clean;
      container.appendChild(wrapper);
      // Use scrollIntoView to avoid clipping of the last line
      setTimeout(() => {
        const last = container.lastElementChild;
        if (last && last.scrollIntoView) last.scrollIntoView({ block: 'end' });
        else container.scrollTop = container.scrollHeight;
      }, 0);
    }
  };

  const appendSystemMessage = (text, kind = 'info') => {
    const rootContainer = richOutputRef.current;
    if (!rootContainer) return;
    const el = document.createElement('div');
    // Support multiple kinds: error, user-input, and default info
    if (kind === 'error') el.className = 'text-red-400';
    else if (kind === 'user-input') el.className = 'text-green-300';
    else el.className = 'text-blue-300';
    el.style.whiteSpace = 'pre-wrap';
    el.textContent = text;
    rootContainer.appendChild(el);
    // ensure fully visible
    setTimeout(() => {
      const last = rootContainer.lastElementChild;
      if (last && last.scrollIntoView) last.scrollIntoView({ block: 'end' });
      else rootContainer.scrollTop = rootContainer.scrollHeight;
    }, 0);
  };

  const sendInput = async (session_id, input) => {
    // Optimistically echo the user's input locally so it appears before
    // any subsequent server-generated prompt / output.
    appendSystemMessage('> ' + input, 'user-input');
    setUserInput('');
    try {
        await apiFetch(`${API_BASE}/input`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id, input }),
        });
    } catch (err) {
      console.error('Failed to send input:', err);
      appendSystemMessage('Failed to send input', 'error');
    }
  };

  // Initial welcome message in the rich output area
  useEffect(() => {
    appendSystemMessage('发种助手 交互输出');
    appendSystemMessage('\n快速开始：\n  1. 在左侧选择一个文件或文件夹\n  2. 在右侧（可选）添加命令行参数\n  3. 点击下方“开始上传”按钮\n');
  }, []);

  const loadBrowseRoots = async () => {
    try {
      const response = await apiFetch(`${API_BASE}/browse_roots`);
      const data = await response.json();

      if (data.success && data.items) {
        setDirectories(data.items);
        setExpandedFolders(new Set());
      }
    } catch (error) {
      console.error('Failed to load browse roots:', error);
    }
  };
  
  // Load description file browser roots
  const loadDescBrowseRoots = async () => {
    try {
      const response = await apiFetch(`${API_BASE}/browse_roots`);
      const data = await response.json();

      if (data.success && data.items) {
        setDescDirectories(data.items);
        setDescExpandedFolders(new Set());
      }
    } catch (error) {
      console.error('Failed to load desc browse roots:', error);
    }
  };
  
  // Load description folder contents
  const loadDescFolderContents = async (path) => {
    try {
      const response = await apiFetch(`${API_BASE}/browse?path=${encodeURIComponent(path)}&filter=desc`);
      const data = await response.json();
      
      if (data.success && data.items) {
        updateDescDirectoryTree(path, data.items);
      }
    } catch (error) {
      console.error('Failed to load desc folder:', error);
    }
  };
  
  // Update description directory tree
  const updateDescDirectoryTree = (path, items) => {
    const updateTree = (nodes) => {
      return nodes.map(node => {
        if (node.path === path) {
          return { ...node, children: items };
        } else if (node.children) {
          return { ...node, children: updateTree(node.children) };
        }
        return node;
      });
    };
    
    setDescDirectories((prev) => updateTree(prev));
  };
  
  // Toggle description folder
  const toggleDescFolder = async (path) => {
    const newExpanded = new Set(descExpandedFolders);

    if (newExpanded.has(path)) {
      newExpanded.delete(path);
      setDescExpandedFolders(newExpanded);
    } else {
      newExpanded.add(path);
      setDescExpandedFolders(newExpanded);
      
      // Show loading indicator while fetching
      setDescLoadingFolders(prev => new Set(prev).add(path));
      try {
        await loadDescFolderContents(path);
      } finally {
        setDescLoadingFolders(prev => {
          const next = new Set(prev);
          next.delete(path);
          return next;
        });
      }
    }
  };
  
  // Load desc roots when --descfile is added
  useEffect(() => {
    if (hasDescFile && descDirectories.length === 0) {
      loadDescBrowseRoots();
    }
  }, [hasDescFile]);
  useEffect(() => {
    storage.set(THEME_KEY, isDarkMode ? 'dark' : 'light');
  }, [isDarkMode]);

  useEffect(() => {
    const handleStorage = (event) => {
      if (event.key === THEME_KEY) {
        setIsDarkMode(event.newValue === 'dark');
      }
    };
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // Mobile resize listener
  useEffect(() => {
    let resizeTimer;
    const handleResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const mobile = window.innerWidth < 768;
        setIsMobile(mobile);
        if (!mobile) setActivePanel('main');
      }, 100);
    };
    window.addEventListener('resize', handleResize);
    return () => {
      clearTimeout(resizeTimer);
      window.removeEventListener('resize', handleResize);
    };
  }, []);

  useEffect(() => {
    loadBrowseRoots();
  }, []);

  // Cleanup file browser search debounce timer on unmount
  useEffect(() => {
    return () => {
      if (fileBrowserSearchTimer.current) {
        clearTimeout(fileBrowserSearchTimer.current);
      }
    };
  }, []);

  // Focus input when executing
  useEffect(() => {
    if (isExecuting && inputRef.current) {
      setTimeout(() => {
        try { inputRef.current.focus(); } catch (e) { /* ignore */ }
      }, 50);
    }
  }, [isExecuting]);

  const toggleFolder = async (path) => {
    const newExpanded = new Set(expandedFolders);
    
    if (newExpanded.has(path)) {
      newExpanded.delete(path);
      setExpandedFolders(newExpanded);
    } else {
      newExpanded.add(path);
      setExpandedFolders(newExpanded);
      
      // Show loading indicator while fetching
      setLoadingFolders(prev => new Set(prev).add(path));
      try {
        await loadFolderContents(path);
      } finally {
        setLoadingFolders(prev => {
          const next = new Set(prev);
          next.delete(path);
          return next;
        });
      }
    }
  };

  const loadFolderContents = async (path) => {
    try {
      const response = await apiFetch(`${API_BASE}/browse?path=${encodeURIComponent(path)}`);
      const data = await response.json();
      
      if (data.success && data.items) {
        updateDirectoryTree(path, data.items);
      }
    } catch (error) {
      console.error('Failed to load folder:', error);
    }
  };

  const updateDirectoryTree = (path, items) => {
    const updateTree = (nodes) => {
      return nodes.map(node => {
        if (node.path === path) {
          return { ...node, children: items };
        } else if (node.children) {
          return { ...node, children: updateTree(node.children) };
        }
        return node;
      });
    };
    
    setDirectories((prev) => updateTree(prev));
  };

  // File Browser search
  const handleFileBrowserSearch = (value) => {
    setFileBrowserSearch(value);
    const searchQuery = value.trim();
    fileBrowserSearchQuery.current = searchQuery;
    if (fileBrowserSearchTimer.current) {
      clearTimeout(fileBrowserSearchTimer.current);
    }
    if (!searchQuery) {
      setFileBrowserSearchResults(null);
      setFileBrowserSearchLoading(false);
      return;
    }
    setFileBrowserSearchLoading(true);
    fileBrowserSearchTimer.current = setTimeout(async () => {
      try {
        const response = await apiFetch(`${API_BASE}/browse_search?q=${encodeURIComponent(searchQuery)}`);
        if (!response.ok) {
          throw new Error(`Search request failed (${response.status})`);
        }
        const data = await response.json();
        // Early return if the search has changed since this request
        if (fileBrowserSearchQuery.current !== searchQuery) return;
        if (data.success) {
          setFileBrowserSearchResults(data);
        } else {
          setFileBrowserSearchResults({ items: [], query: searchQuery, count: 0 });
        }
      } catch (error) {
        console.error('File browser search failed:', error);
        if (fileBrowserSearchQuery.current === searchQuery) {
          setFileBrowserSearchResults({ items: [], query: searchQuery, count: 0 });
        }
      } finally {
        if (fileBrowserSearchQuery.current === searchQuery) {
          setFileBrowserSearchLoading(false);
        }
      }
    }, 300); //300ms debounce so we dont spam requests for every keystroke
  };

  const renderSearchResults = (results) => {
    if (!results || !results.items) return null;
    if (results.items.length === 0) {
      return (
        <div className={`p-4 text-center ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
          <p className="text-sm">No results found</p>
        </div>
      );
    }
    return results.items.map((item, idx) => {
      const separatorIdx = Math.max(item.path.lastIndexOf('/'), item.path.lastIndexOf('\\'));
      const parentPath = separatorIdx > 0 ? item.path.substring(0, separatorIdx) : '';
      return (
        <div key={idx}>
          <div
            className={`flex items-center gap-2 px-3 ${isMobile ? 'py-3' : 'py-2'} cursor-pointer transition-colors ${
              selectedPath === item.path
                ? isDarkMode
                  ? 'bg-purple-900 border-l-4 border-purple-500'
                  : 'bg-blue-100 border-l-4 border-blue-500'
                : isDarkMode
                  ? 'hover:bg-gray-700'
                  : 'hover:bg-gray-100'
            }`}
            style={{ paddingLeft: '12px' }}
            onClick={() => {
              setSelectedPath(item.path);
              setSelectedName(item.name);
              if (isMobile) setActivePanel('main');
            }}
          >
            <span className={`flex-shrink-0 ${item.type === 'folder' ? 'text-yellow-600' : 'text-blue-600'}`}>
              {item.type === 'folder' ? <FolderIcon /> : <FileIcon />}
            </span>
            <div className="flex flex-col min-w-0">
              <span className={`text-sm font-medium ${isDarkMode ? 'text-gray-200' : 'text-gray-700'} truncate`}>
                {item.name}
              </span>
              <span className={`text-xs ${isDarkMode ? 'text-gray-500' : 'text-gray-400'} truncate`} title={parentPath}>
                {parentPath}
              </span>
            </div>
          </div>
        </div>
      );
    });
  };

  const renderFileTree = (items, level = 0) => {
    return items.map((item, idx) => {
      const isLoading = item.type === 'folder' && loadingFolders.has(item.path);
      return (
        <div key={idx}>
          <div
            className={`flex items-center gap-2 px-3 ${isMobile ? 'py-3' : 'py-2'} cursor-pointer transition-colors ${
              selectedPath === item.path 
                ? isDarkMode 
                  ? 'bg-purple-900 border-l-4 border-purple-500' 
                  : 'bg-blue-100 border-l-4 border-blue-500'
                : isDarkMode
                  ? 'hover:bg-gray-700'
                  : 'hover:bg-gray-100'
            }`}
            style={{ paddingLeft: `${level * 20 + 12}px` }}
            onClick={() => {
              if (item.type === 'folder') {
                toggleFolder(item.path);
              }
              setSelectedPath(item.path);
              setSelectedName(item.name);
              if (isMobile && item.type !== 'folder') setActivePanel('main');
            }}
          >
            <span className={`flex-shrink-0 ${isLoading ? 'text-purple-500' : 'text-yellow-600'}`}>
              {item.type === 'folder' ? (
                isLoading ? <SpinnerIcon /> : (expandedFolders.has(item.path) ? <FolderOpenIcon /> : <FolderIcon />)
              ) : (
                <span className="text-blue-600"><FileIcon /></span>
              )}
            </span>
            <div className="flex flex-col min-w-0">
              <span className={`text-sm font-medium ${isDarkMode ? 'text-gray-200' : 'text-gray-700'} truncate`}>
                {item.name}
                {isLoading && <span className={`ml-2 text-xs ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>Loading...</span>}
              </span>
              {item.subtitle && (
                <span className={`text-xs ${isDarkMode ? 'text-gray-500' : 'text-gray-400'} truncate`} title={item.subtitle}>{item.subtitle}</span>
              )}
            </div>
          </div>
          {item.type === 'folder' && expandedFolders.has(item.path) && item.children && item.children.length > 0 && (
            <div>{renderFileTree(item.children, level + 1)}</div>
          )}
        </div>
      );
    });
  };
  
  // Render description file tree
  const renderDescFileTree = (items, level = 0) => {
    return items.map((item, idx) => {
      const isLoading = item.type === 'folder' && descLoadingFolders.has(item.path);
      return (
        <div key={idx}>
          <div
            className={`flex items-center gap-2 px-3 ${isMobile ? 'py-3' : 'py-2'} cursor-pointer transition-colors ${
              descFilePath === item.path 
                ? isDarkMode 
                  ? 'bg-green-900 border-l-4 border-green-500' 
                  : 'bg-green-100 border-l-4 border-green-500'
                : isDarkMode
                  ? 'hover:bg-gray-700'
                  : 'hover:bg-gray-100'
            }`}
            style={{ paddingLeft: `${level * 20 + 12}px` }}
            onClick={() => {
              if (item.type === 'folder') {
                toggleDescFolder(item.path);
              } else {
                // Update the argument directly with the selected file path
                updateDescFile(item.path);
              }
            }}
          >
            <span className={`flex-shrink-0 ${isLoading ? 'text-green-500' : 'text-yellow-600'}`}>
              {item.type === 'folder' ? (
                isLoading ? <SpinnerIcon /> : (descExpandedFolders.has(item.path) ? <FolderOpenIcon /> : <FolderIcon />)
              ) : (
                <span className="text-green-600"><FileIcon /></span>
              )}
            </span>
            <div className="flex flex-col min-w-0">
              <span className={`text-sm font-medium ${isDarkMode ? 'text-gray-200' : 'text-gray-700'} truncate`}>
                {item.name}
                {isLoading && <span className={`ml-2 text-xs ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>Loading...</span>}
              </span>
              {item.subtitle && (
                <span className={`text-xs ${isDarkMode ? 'text-gray-500' : 'text-gray-400'} truncate`} title={item.subtitle}>{item.subtitle}</span>
              )}
            </div>
          </div>
          {item.type === 'folder' && descExpandedFolders.has(item.path) && item.children && item.children.length > 0 && (
            <div>{renderDescFileTree(item.children, level + 1)}</div>
          )}
        </div>
      );
    });
  };

  const executeCommand = async () => {
    if (!selectedPath) {
      appendSystemMessage('✗ Please select a file or folder first', 'error');
      return;
    }
    
    // Validate --descfile: must have a valid description file path
    if (hasDescFile) {
      if (!descFilePath) {
        appendSystemMessage('✗ Please select or enter a description file path when using --descfile', 'error');
        return;
      }
      const pathValidation = isValidDescFilePath(descFilePath);
      if (!pathValidation.valid) {
        appendSystemMessage(`✗ Invalid description file: ${pathValidation.error}`, 'error');
        return;
      }
    }
    
    // Validate --desclink: must have a valid URL
    if (hasDescLink) {
      if (!descLinkUrl) {
        appendSystemMessage('✗ Please enter a description URL when using --desclink', 'error');
        return;
      }
      if (!isValidUrl(descLinkUrl)) {
        appendSystemMessage('✗ Please enter a valid paste URL for --desclink (pastebin, hastebin, etc.)', 'error');
        return;
      }
    }

    const rootContainer = richOutputRef.current;
    if (!rootContainer) return;

    const newSessionId = 'session_' + Date.now();
    setSessionId(newSessionId);
    setIsExecuting(true);
    // Clear the initial welcome text so execution output appears immediately
    if (rootContainer) {
      rootContainer.innerHTML = '';
    }
    // Reset last-full snapshot key to allow appending fresh full snapshots
    if (lastFullHashRef) lastFullHashRef.current = '';

    appendSystemMessage('');
    appendSystemMessage(`$ python upload.py "${selectedPath}" ${customArgs}`);
    appendSystemMessage('→ Starting execution...');

    // Local controller binding for this run. Declare here so it's visible
    // to `catch`/`finally` blocks and inner callbacks.
    let localController = null;

    try {
      // Replace any existing controller to avoid reusing an aborted signal.
      const controller = new AbortController();
      sseAbortControllerRef.current = controller;
      // Bind a local controller reference for this execution run to avoid
      // races if another run replaces the shared ref concurrently.
      localController = controller;

      const response = await apiFetch(`${API_BASE}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: selectedPath,
          args: customArgs,
          session_id: newSessionId
        })
      , signal: controller.signal
      });

      if (!response.ok) {
        const errText = await response.text();
        appendSystemMessage(`✗ Execute failed (${response.status}): ${errText || 'Request failed'}`, 'error');
        return;
      }
      if (!response.body) {
        appendSystemMessage('✗ Execute failed: empty response body', 'error');
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      const processSSELine = (line) => {
        if (localController && localController.signal.aborted) return;
        if (!line.trim() || !line.startsWith('data: ')) return;
        try {
          const data = JSON.parse(line.substring(6));
          if (data.type === 'html' || data.type === 'html_full') {
            try {
              const rawHtml = data.data || '';
              const clean = sanitizeHtml(rawHtml);
              if (data.type === 'html_full') {
                const shortSample = clean.slice(0, 200);
                const key = `${clean.length}:${shortSample}`;
                if (lastFullHashRef.current !== key) {
                  lastFullHashRef.current = key;
                  const wrapper = document.createElement('div');
                  wrapper.innerHTML = clean;
                  if (rootContainer) rootContainer.appendChild(wrapper);
                  setTimeout(() => {
                    const last = rootContainer && rootContainer.lastElementChild;
                    if (last && last.scrollIntoView) last.scrollIntoView({ block: 'end' });
                    else if (rootContainer) rootContainer.scrollTop = rootContainer.scrollHeight;
                  }, 0);
                }
                return;
              }
              // delegate to shared helper for fragments
              appendHtmlFragment(clean);
            } catch (e) {
              console.error('Failed to render HTML fragment:', e);
            }
              } else if (data.type === 'exit') {
            if (!(localController && localController.signal.aborted)) {
              appendSystemMessage('');
              appendSystemMessage(`✓ Process exited with code ${data.code}`);
            }
          }
        } catch (e) {
          console.error('Parse error:', e);
        }
      };

      /* eslint-disable no-constant-condition */
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          // process any remaining buffered content
          if (buffer) {
            const finalLines = buffer.split('\n');
            for (const line of finalLines) {
              processSSELine(line);
            }
          }
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n');
        buffer = parts.pop(); // last item may be incomplete

        for (const line of parts) {
          processSSELine(line);
        }
      }
      /* eslint-enable no-constant-condition */
      // Only append the final completion message when not aborted.
      if (!(localController && localController.signal.aborted)) {
        appendSystemMessage('✓ Execution completed');
        appendSystemMessage('');
      }
    } catch (error) {
      // Suppress abort errors as they are expected when a user cancels.
      if (!(localController && localController.signal.aborted)) {
        appendSystemMessage('✗ Execution error: ' + error.message, 'error');
      }
    } finally {
      setIsExecuting(false);
      setSessionId('');
      // Clear controller reference when finished, but only if it hasn't been
      // replaced by another concurrent run.
      try {
        if (sseAbortControllerRef.current === localController) {
          sseAbortControllerRef.current = null;
        }
      } catch (e) { /* ignore */ }
    }
  };

  const clearTerminal = async () => {
    // If a process is running, kill it first
    if (isExecuting && sessionId) {
      try {
        // Abort the SSE fetch so the client stops processing incoming events
        if (sseAbortControllerRef.current) {
          try { sseAbortControllerRef.current.abort(); } catch (e) { /* ignore */ }
        }
        await apiFetch(`${API_BASE}/kill`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId })
        });

        appendSystemMessage('✗ Process terminated by user', 'error');

        setIsExecuting(false);
        setSessionId('');
      } catch (error) {
        console.error('Failed to kill process:', error);
      }
    }

    // Clear the rich output container
    const container = richOutputRef.current;
    if (container) {
      container.innerHTML = '';
      appendSystemMessage('发种助手 交互输出');
      appendSystemMessage('\n快速开始：\n  1. 在左侧选择一个文件或文件夹\n  2. 在右侧（可选）添加命令行参数\n  3. 点击下方“开始上传”按钮\n');
    }
  };

  // Sidebar resizing
  const startResizing = useCallback(() => {
    setIsResizing(true);
  }, [setIsResizing]);

  const stopResizing = useCallback(() => {
    setIsResizing(false);
  }, [setIsResizing]);

  const resize = useCallback((e) => {
    const newWidth = e.clientX;
    if (newWidth >= 200 && newWidth <= 600) {
      setSidebarWidth(newWidth);
    }
  }, [setSidebarWidth]);

  useEffect(() => {
    if (isResizing) {
      window.addEventListener('mousemove', resize);
      window.addEventListener('mouseup', stopResizing);
      return () => {
        window.removeEventListener('mousemove', resize);
        window.removeEventListener('mouseup', stopResizing);
      };
    }
  }, [isResizing, resize, stopResizing]);

  // Right sidebar resizing
  const startResizingRight = useCallback(() => {
    setIsResizingRight(true);
  }, [setIsResizingRight]);

  const stopResizingRight = useCallback(() => {
    setIsResizingRight(false);
  }, [setIsResizingRight]);

  const resizeRight = useCallback((e) => {
    // Calculate width from right edge
    const newWidth = window.innerWidth - e.clientX;
    if (newWidth >= 200 && newWidth <= 800) {
      setRightSidebarWidth(newWidth);
    }
  }, [setRightSidebarWidth]);

  useEffect(() => {
    if (isResizingRight) {
      window.addEventListener('mousemove', resizeRight);
      window.addEventListener('mouseup', stopResizingRight);
      return () => {
        window.removeEventListener('mousemove', resizeRight);
        window.removeEventListener('mouseup', stopResizingRight);
      };
    }
  }, [isResizingRight, resizeRight, stopResizingRight]);

  // argumentCategories moved to module scope

  // Append only the plain argument flag to the input (no example values)
  const addArgument = (arg) => {
    setCustomArgs((prev) => (prev && prev.length ? `${prev} ${arg}` : arg));
  };

  // Toggle section collapse
  const toggleSectionCollapse = (title) => {
    setCollapsedSections((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(title)) {
        newSet.delete(title);
      } else {
        newSet.add(title);
      }
      return newSet;
    });
  };

  // Collapse all sections
  const collapseAllSections = () => {
    setCollapsedSections(new Set(argumentCategories.map((cat) => cat.title)));
  };

  // Expand all sections
  const expandAllSections = () => {
    setCollapsedSections(new Set());
  };

  // Filter argument categories based on search
  const getFilteredCategories = () => {
    if (!argSearchFilter.trim()) {
      return argumentCategories;
    }
    const searchLower = argSearchFilter.toLowerCase();
    return argumentCategories
      .map((cat) => {
        const filteredArgs = cat.args.filter(
          (a) =>
            a.label.toLowerCase().includes(searchLower) ||
            (a.description && a.description.toLowerCase().includes(searchLower)) ||
            (a.placeholder && a.placeholder.toLowerCase().includes(searchLower))
        );
        if (filteredArgs.length > 0) {
          return { ...cat, args: filteredArgs };
        }
        // Also include category if title matches
        if (cat.title.toLowerCase().includes(searchLower)) {
          return cat;
        }
        return null;
      })
      .filter(Boolean);
  };

  const filteredCategories = getFilteredCategories();

  // Mobile Layout
  if (isMobile) {
    const navButton = (panel, icon, label) => (
      <button
        key={panel}
        onClick={() => setActivePanel(panel)}
        className={`flex-1 flex flex-col items-center justify-center gap-1 py-2 transition-colors ${
          activePanel === panel
            ? 'text-purple-400 border-t-2 border-purple-400'
            : isDarkMode ? 'text-gray-400' : 'text-gray-500'
        }`}
      >
        {icon}
        <span className="text-xs font-medium">{label}</span>
      </button>
    );

    return (
      <div className={`flex flex-col h-screen ${isDarkMode ? 'bg-gray-900' : 'bg-gray-50'}`}>
        {/* Mobile Header */}
        <div className={`flex items-center justify-between px-4 py-3 border-b flex-shrink-0 ${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'}`}>
          <h1 className={`text-lg font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
            <UploadIcon />
            发种助手
          </h1>
          <div className="flex items-center gap-2">
            <a
              href={`${APP_BASE}/config`}
              className="px-2 py-1 rounded text-xs font-semibold bg-blue-600 text-white hover:bg-blue-700"
            >
              Config
            </a>
            <button
              onClick={() => setIsDarkMode(!isDarkMode)}
              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${isDarkMode ? 'bg-purple-600' : 'bg-gray-300'}`}
            >
              <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${isDarkMode ? 'translate-x-5' : 'translate-x-1'}`} />
            </button>
            <a
              href={`${APP_BASE}/logout`}
              className="px-2 py-1 rounded text-xs font-semibold bg-red-600 text-white hover:bg-red-700"
            >
              Logout
            </a>
          </div>
        </div>

        {/* Mobile Content Area. Main panel always mounted (hidden when inactive) to preserve terminal output ref; Files and Args panels conditionally rendered */}
        <div className="flex-1 overflow-hidden relative">
          {/* Files Panel */}
          {activePanel === 'files' && (
            <div className="flex flex-col h-full">
              <div className={`p-3 border-b flex-shrink-0 ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-r from-purple-50 to-blue-50'}`}>
                <h2 className={`text-base font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
                  <FolderIcon />
                  文件浏览器
                </h2>
                <div className="relative mt-2">
                  <input
                    type="text"
                    value={fileBrowserSearch}
                    onChange={(e) => handleFileBrowserSearch(e.target.value)}
                    placeholder="搜索文件和文件夹..."
                    className={`w-full pl-8 pr-8 py-1.5 text-sm rounded border ${
                      isDarkMode
                        ? 'bg-gray-800 border-gray-600 text-gray-200 placeholder-gray-500 focus:border-purple-500'
                        : 'bg-white border-gray-300 text-gray-700 placeholder-gray-400 focus:border-blue-500'
                    } focus:outline-none focus:ring-1 ${isDarkMode ? 'focus:ring-purple-500' : 'focus:ring-blue-500'}`}
                  />
                  <svg className={`absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 ${isDarkMode ? 'text-gray-500' : 'text-gray-400'}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                  </svg>
                  {fileBrowserSearch && (
                    <button
                      onClick={() => handleFileBrowserSearch('')}
                      className={`absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
              <div className={`flex-1 overflow-y-auto ${hasDescFile && !descBrowserCollapsed ? 'max-h-[50%]' : ''}`}>
                {fileBrowserSearch ? (
                  fileBrowserSearchLoading ? (
                    <div className={`p-4 text-center ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                      <SpinnerIcon />
                      <p className="text-sm mt-2">Searching...</p>
                    </div>
                  ) : (
                    <>
                      {fileBrowserSearchResults && fileBrowserSearchResults.truncated && (
                        <div className={`px-3 py-1.5 text-xs ${isDarkMode ? 'text-yellow-400 bg-gray-900' : 'text-yellow-700 bg-yellow-50'} border-b ${isDarkMode ? 'border-gray-700' : 'border-yellow-200'}`}>
                          Results limited to {fileBrowserSearchResults.count} items
                        </div>
                      )}
                      {renderSearchResults(fileBrowserSearchResults)}
                    </>
                  )
                ) : (
                  renderFileTree(directories)
                )}
              </div>

              {/* Description File Browser */}
              {hasDescFile && (
                <>
                  <div
                    className={`p-3 border-t flex-shrink-0 ${!descBrowserCollapsed ? 'border-b' : ''} ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-r from-green-50 to-emerald-50'} ${descBrowserCollapsed ? 'cursor-pointer' : ''}`}
                    onClick={descBrowserCollapsed ? () => setDescBrowserCollapsed(false) : undefined}
                  >
                    <div className="flex items-center justify-between">
                      <h2 className={`text-base font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
                        <FileIcon />
                        Description File
                        {descBrowserCollapsed && descFilePath && !descFileError && (
                          <span className="text-green-500 ml-1">
                            <svg className="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          </span>
                        )}
                      </h2>
                      {descBrowserCollapsed ? (
                        <button
                          onClick={(e) => { e.stopPropagation(); setDescBrowserCollapsed(false); }}
                          className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                        >
                          <ChevronDownIcon />
                        </button>
                      ) : descFilePath && !descFileError && (
                        <button
                          onClick={() => setDescBrowserCollapsed(true)}
                          className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                        >
                          <ChevronRightIcon />
                        </button>
                      )}
                    </div>
                    {descBrowserCollapsed && descFilePath ? (
                      <div className="flex items-center gap-2 mt-1">
                        <p className={`text-xs ${descFileError ? (isDarkMode ? 'text-red-400' : 'text-red-600') : (isDarkMode ? 'text-green-400' : 'text-green-700')} break-all font-mono flex-1`}>{descFilePath}</p>
                        <button
                          onClick={(e) => { e.stopPropagation(); updateDescFile(''); setDescBrowserCollapsed(false); }}
                          className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                        >
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </div>
                    ) : !descBrowserCollapsed && (
                      <p className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                        Select a .txt, .nfo, or .md file
                      </p>
                    )}
                  </div>
                  {!descBrowserCollapsed && (
                    <div className="flex-1 overflow-y-auto">
                      {descDirectories.length > 0 ? renderDescFileTree(descDirectories) : (
                        <div className={`p-4 text-center ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                          <p className="text-sm">Loading description files...</p>
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Main Upload Panel */}
          <div className={`flex flex-col h-full ${activePanel === 'main' ? '' : 'hidden'}`}>
              {/* Top controls */}
              <div className={`p-3 space-y-3 border-b flex-shrink-0 ${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'}`}>
                {selectedPath ? (
                  <div className={`p-2 rounded-lg ${isDarkMode ? 'bg-gray-700 border-gray-600' : 'bg-blue-50 border-blue-200'} border`}>
                    <div className="flex items-center justify-between">
                      <p className={`text-xs font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-600'}`}>Selected:</p>
                      <button
                        onClick={() => setActivePanel('files')}
                        className={`text-xs px-2 py-0.5 rounded ${isDarkMode ? 'bg-gray-600 text-gray-200 hover:bg-gray-500' : 'bg-blue-100 text-blue-700 hover:bg-blue-200'}`}
                      >
                        Browse
                      </button>
                    </div>
                    <p className={`text-xs ${isDarkMode ? 'text-white' : 'text-gray-800'} break-all font-mono mt-1`}>{selectedPath}</p>
                  </div>
                ) : (
                  <button
                    onClick={() => setActivePanel('files')}
                    className={`w-full p-3 rounded-lg border-2 border-dashed text-center inline-flex items-center justify-center ${isDarkMode ? 'border-gray-600 text-gray-400 hover:border-purple-500 hover:text-purple-400' : 'border-gray-300 text-gray-500 hover:border-purple-500 hover:text-purple-600'}`}
                  >
                    <FolderIcon />
                    <span className="text-sm ml-2">Tap to select a file or folder</span>
                  </button>
                )}

                {/* Args input */}
                <input
                  type="text"
                  value={customArgs}
                  onChange={(e) => setCustomArgs(e.target.value)}
                  placeholder="--tmdb movie/12345 --trackers ptp,aither"
                  className={`w-full px-3 py-2 text-sm border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                    isDarkMode
                      ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400'
                      : 'bg-white border-gray-300 text-gray-900'
                  }`}
                  disabled={isExecuting}
                />

                {/* Desc Link Input */}
                {hasDescLink && (!descLinkUrl || descLinkFocused || descLinkError) && (
                  <div className="space-y-1">
                    <label className={`text-xs font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-700'}`}>Description Link URL:</label>
                    <input
                      type="url"
                      value={descLinkUrl}
                      onChange={(e) => updateDescLink(e.target.value)}
                      onFocus={() => setDescLinkFocused(true)}
                      onBlur={() => setDescLinkFocused(false)}
                      placeholder="https://pastebin.com/abc123"
                      className={`w-full px-3 py-2 text-sm border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                        descLinkError
                          ? 'border-red-500 focus:ring-red-500'
                          : isDarkMode
                            ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400'
                            : 'bg-white border-gray-300 text-gray-900'
                      }`}
                      disabled={isExecuting}
                    />
                    {descLinkError && <p className="text-xs text-red-500">{descLinkError}</p>}
                  </div>
                )}

                {hasDescFile && (descFileError || !descFilePath) && (
                  <div className={`p-2 rounded-lg text-xs ${
                    descFileError
                      ? isDarkMode ? 'bg-red-900 border border-red-700 text-red-300' : 'bg-red-50 border border-red-200 text-red-700'
                      : isDarkMode ? 'bg-yellow-900 border border-yellow-700 text-yellow-300' : 'bg-yellow-50 border border-yellow-200 text-yellow-700'
                  }`}>
                    {descFileError || 'Select a description file from the Files panel'}
                  </div>
                )}

                {/* Execute & Kill buttons */}
                <div className="flex gap-2">
                  <button
                    onClick={executeCommand}
                    disabled={!selectedPath || isExecuting}
                    className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:bg-gray-400 disabled:cursor-not-allowed transition-colors font-medium"
                  >
                    <PlayIcon />
                    {isExecuting ? '执行中...' : '开始上传'}
                  </button>
                  <button
                    onClick={clearTerminal}
                    className={`flex items-center gap-1 px-3 py-3 rounded-lg transition-colors ${
                      isExecuting
                        ? 'bg-red-600 hover:bg-red-700 text-white'
                        : 'bg-gray-600 hover:bg-gray-700 text-white'
                    }`}
                    title={isExecuting ? 'Kill process and clear terminal' : 'Clear terminal'}
                  >
                    <TrashIcon />
                  </button>
                </div>
              </div>

              {/* Terminal output */}
              <div className={`flex-1 p-3 flex flex-col min-h-0 overflow-hidden ${isDarkMode ? 'bg-gray-900' : 'bg-gray-100'}`}>
                <div className="flex items-center gap-2 mb-2 flex-shrink-0">
                  <span className={isDarkMode ? 'text-white' : 'text-gray-800'}><TerminalIcon /></span>
                  <h3 className={`text-sm font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'}`}>输出</h3>
                  {isExecuting && (
                    <span className="ml-auto text-xs text-green-400 animate-pulse">● Running</span>
                  )}
                </div>
                <div
                  ref={richOutputRef}
                  id="rich-output"
                  className={`flex-1 rounded-lg overflow-auto p-2 border text-sm ${isDarkMode ? 'bg-gray-900 border-gray-700 text-white' : 'bg-white border-gray-200 text-gray-900'}`}
                ></div>
                {isExecuting && (
                  <div className="mt-2 flex gap-2">
                    <input
                      ref={inputRef}
                      value={userInput}
                      onChange={(e) => setUserInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); sendInput(sessionId, userInput); } }}
                      placeholder="Type input and press Enter"
                      className={`flex-1 px-3 py-2 text-sm rounded-lg border focus:ring-2 focus:ring-purple-500 focus:border-transparent ${isDarkMode ? 'bg-gray-700 border-gray-600 text-white' : 'bg-white border-gray-300 text-gray-900'}`}
                    />
                    <button
                      onClick={() => sendInput(sessionId, userInput)}
                      disabled={!sessionId || !userInput}
                      className="px-3 py-2 rounded-lg bg-green-600 text-white hover:bg-green-700 disabled:opacity-50 text-sm"
                    >
                      Send
                    </button>
                  </div>
                )}
              </div>
            </div>

          {/* Args Panel */}
          {activePanel === 'args' && (
            <div className="flex flex-col h-full">
              <div className={`p-3 border-b flex-shrink-0 ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-l from-purple-50 to-blue-50'}`}>
                <h2 className={`text-base font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
                  <TerminalIcon />
                  Arguments
                </h2>
              </div>

              {/* Search and Collapse Controls */}
              <div className={`p-3 border-b flex-shrink-0 ${isDarkMode ? 'border-gray-700' : 'border-gray-200'} space-y-2`}>
                <div className="relative">
                  <div className={`absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                    <SearchIcon />
                  </div>
                  <input
                    type="text"
                    value={argSearchFilter}
                    onChange={(e) => setArgSearchFilter(e.target.value)}
                    placeholder="Search arguments..."
                    className={`w-full pl-10 pr-3 py-2 text-sm border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                      isDarkMode
                        ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400'
                        : 'bg-white border-gray-300 text-gray-900 placeholder-gray-500'
                    }`}
                  />
                  {argSearchFilter && (
                    <button
                      onClick={() => setArgSearchFilter('')}
                      className={`absolute inset-y-0 right-0 pr-3 flex items-center ${isDarkMode ? 'text-gray-400 hover:text-gray-200' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={collapseAllSections}
                    className={`flex-1 flex items-center justify-center gap-1 px-2 py-1.5 text-xs font-medium rounded-md border transition-colors ${
                      isDarkMode
                        ? 'bg-gray-700 border-gray-600 text-gray-300 hover:bg-gray-600'
                        : 'bg-gray-100 border-gray-300 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    <CollapseAllIcon />
                    Collapse All
                  </button>
                  <button
                    onClick={expandAllSections}
                    className={`flex-1 flex items-center justify-center gap-1 px-2 py-1.5 text-xs font-medium rounded-md border transition-colors ${
                      isDarkMode
                        ? 'bg-gray-700 border-gray-600 text-gray-300 hover:bg-gray-600'
                        : 'bg-gray-100 border-gray-300 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    <ExpandAllIcon />
                    Expand All
                  </button>
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-3 space-y-2">
                {filteredCategories.length === 0 ? (
                  <div className={`text-center py-8 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                    <p className="text-sm">No arguments found matching "{argSearchFilter}"</p>
                  </div>
                ) : (
                  filteredCategories.map((cat) => (
                    <div key={cat.title} className={`rounded-lg border ${isDarkMode ? 'border-gray-700' : 'border-gray-200'}`}>
                      <button
                        onClick={() => toggleSectionCollapse(cat.title)}
                        className={`w-full flex items-center justify-between p-3 text-left transition-colors rounded-t-lg ${
                          isDarkMode ? 'hover:bg-gray-700' : 'hover:bg-gray-50'
                        } ${collapsedSections.has(cat.title) ? 'rounded-b-lg' : ''}`}
                      >
                        <div className="flex-1">
                          <div className={`text-sm font-bold ${isDarkMode ? 'text-gray-100' : 'text-gray-900'} flex items-center gap-2`}>
                            <span className={isDarkMode ? 'text-gray-400' : 'text-gray-500'}>
                              {collapsedSections.has(cat.title) ? <ChevronRightIcon /> : <ChevronDownIcon />}
                            </span>
                            {cat.title}
                            <span className={`text-xs font-normal px-1.5 py-0.5 rounded ${isDarkMode ? 'bg-gray-700 text-gray-400' : 'bg-gray-200 text-gray-500'}`}>
                              {cat.args.length}
                            </span>
                          </div>
                          {cat.subtitle && !collapsedSections.has(cat.title) && (
                            <div className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>{cat.subtitle}</div>
                          )}
                        </div>
                      </button>
                      {!collapsedSections.has(cat.title) && (
                        <div className={`px-3 pb-3 pt-2 ${isDarkMode ? 'border-t border-gray-700' : 'border-t border-gray-200'}`}>
                          <div className="grid grid-cols-1 gap-2">
                            {cat.args.map((a) => (
                              <div
                                key={a.label}
                                className={`w-full p-2 rounded-lg border ${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-100'}`}
                              >
                                <div className="flex items-start justify-between gap-3">
                                  <button
                                    onClick={() => addArgument(a.label)}
                                    disabled={isExecuting}
                                    className={`px-3 py-1.5 text-sm font-mono rounded-md border ${isDarkMode ? 'bg-gray-700 border-gray-600 text-white hover:bg-purple-600 hover:text-white' : 'bg-white border-gray-200 text-gray-800 hover:bg-purple-600 hover:text-white'} transition-colors`}
                                  >
                                    {a.label}
                                  </button>
                                  <div className="flex-1 text-right">
                                    {a.placeholder && (
                                      <div className={`text-xs ${isDarkMode ? 'text-gray-300' : 'text-gray-500'} font-mono`}>{a.placeholder}</div>
                                    )}
                                  </div>
                                </div>
                                {a.description && (
                                  <div className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>{a.description}</div>
                                )}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>

        {/* Bottom Nav */}
        <div className={`flex border-t flex-shrink-0 ${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'}`}>
          {navButton('files', <FolderIcon />, '文件')}
          {navButton('main', <UploadIcon />, '上传')}
          {navButton('args', <TerminalIcon />, '参数')}
        </div>
      </div>
    );
  }

  // Desktop Layout
  return (
    <div className={`flex h-screen ${isDarkMode ? 'bg-gray-900' : 'bg-gray-50'} overflow-hidden`}>
      {/* Left Sidebar - Resizable */}
      <div 
        className={`${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'} border-r flex flex-col`}
        style={{ width: `${sidebarWidth}px`, minWidth: '200px', maxWidth: '600px' }}
      >
        <div className={`p-4 border-b ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-r from-purple-50 to-blue-50'}`}>
          <h2 className={`text-lg font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
            <FolderIcon />
            文件浏览器
          </h2>
          <div className="relative mt-2">
            <input
              type="text"
              value={fileBrowserSearch}
              onChange={(e) => handleFileBrowserSearch(e.target.value)}
              placeholder="搜索文件和文件夹..."
              className={`w-full pl-8 pr-8 py-1.5 text-sm rounded border ${
                isDarkMode
                  ? 'bg-gray-800 border-gray-600 text-gray-200 placeholder-gray-500 focus:border-purple-500'
                  : 'bg-white border-gray-300 text-gray-700 placeholder-gray-400 focus:border-blue-500'
              } focus:outline-none focus:ring-1 ${isDarkMode ? 'focus:ring-purple-500' : 'focus:ring-blue-500'}`}
            />
            <svg className={`absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 ${isDarkMode ? 'text-gray-500' : 'text-gray-400'}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            {fileBrowserSearch && (
              <button
                onClick={() => handleFileBrowserSearch('')}
                className={`absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        </div>
        <div className={`${hasDescFile && !descBrowserCollapsed ? 'flex-1 max-h-[50%]' : 'flex-1'} overflow-y-auto`}>
          {fileBrowserSearch ? (
            fileBrowserSearchLoading ? (
              <div className={`p-4 text-center ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                <SpinnerIcon />
                <p className="text-sm mt-2">Searching...</p>
              </div>
            ) : (
              <>
                {fileBrowserSearchResults && fileBrowserSearchResults.truncated && (
                  <div className={`px-3 py-1.5 text-xs ${isDarkMode ? 'text-yellow-400 bg-gray-900' : 'text-yellow-700 bg-yellow-50'} border-b ${isDarkMode ? 'border-gray-700' : 'border-yellow-200'}`}>
                    Results limited to {fileBrowserSearchResults.count} items
                  </div>
                )}
                {renderSearchResults(fileBrowserSearchResults)}
              </>
            )
          ) : (
            renderFileTree(directories)
          )}
        </div>
        
        {/* Description File Browser - shown when --descfile is in args */}
        {hasDescFile && (
          <>
            <div 
              className={`p-4 border-t ${!descBrowserCollapsed ? 'border-b' : ''} ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-r from-green-50 to-emerald-50'} ${descBrowserCollapsed ? 'cursor-pointer' : ''}`}
              onClick={descBrowserCollapsed ? () => setDescBrowserCollapsed(false) : undefined}
            >
              <div className="flex items-center justify-between">
                <h2 className={`text-lg font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
                  <FileIcon />
                  Description File
                  {descBrowserCollapsed && descFilePath && !descFileError && (
                    <span className="text-green-500 ml-1">
                      <svg className="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                    </span>
                  )}
                </h2>
                {descBrowserCollapsed ? (
                  <button
                    onClick={(e) => { e.stopPropagation(); setDescBrowserCollapsed(false); }}
                    className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                    title="Expand browser"
                  >
                    <ChevronDownIcon />
                  </button>
                ) : descFilePath && !descFileError && (
                  <button
                    onClick={() => setDescBrowserCollapsed(true)}
                    className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                    title="Collapse browser"
                  >
                    <ChevronRightIcon />
                  </button>
                )}
              </div>
              {descBrowserCollapsed && descFilePath ? (
                <div className="flex items-center gap-2 mt-2">
                  <p className={`text-xs ${descFileError ? (isDarkMode ? 'text-red-400' : 'text-red-600') : (isDarkMode ? 'text-green-400' : 'text-green-700')} break-all font-mono flex-1`}>{descFilePath}</p>
                  <button
                    onClick={(e) => { e.stopPropagation(); updateDescFile(''); setDescBrowserCollapsed(false); }}
                    className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                    title="Clear selection"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              ) : !descBrowserCollapsed && (
                <p className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                  Select a .txt, .nfo, or .md file
                </p>
              )}
            </div>
            {!descBrowserCollapsed && (
              <>
                <div className="flex-1 overflow-y-auto">
                  {descDirectories.length > 0 ? (
                    renderDescFileTree(descDirectories)
                  ) : (
                    <div className={`p-4 text-center ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
                      <p className="text-sm">Loading description files...</p>
                    </div>
                  )}
                </div>
                {descFilePath && (
                  <div className={`p-3 border-t ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-green-50'}`}>
                    <p className={`text-xs font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-600'} mb-1`}>Selected Description:</p>
                    <div className="flex items-center gap-2">
                      <p className={`text-xs ${isDarkMode ? 'text-green-400' : 'text-green-700'} break-all font-mono flex-1`}>{descFilePath}</p>
                      <button
                        onClick={() => updateDescFile('')}
                        className={`p-1 rounded ${isDarkMode ? 'hover:bg-gray-700 text-gray-400' : 'hover:bg-gray-200 text-gray-500'}`}
                        title="Clear selection"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>

      {/* Resize Handle */}
      <div
        className={`w-1 ${isDarkMode ? 'bg-gray-700 hover:bg-purple-500' : 'bg-gray-300 hover:bg-purple-500'} cursor-col-resize transition-colors`}
        onMouseDown={startResizing}
        style={{ userSelect: 'none' }}
      />

      {/* Main Content */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Top Panel */}
        <div className={`${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'} border-b p-4 flex-shrink-0`}>
          <div className="max-w-6xl mx-auto space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <h1 className={`text-2xl font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
                  <UploadIcon />
                  发种助手 Web UI
                </h1>
                <a
                  href={`${APP_BASE}/logout`}
                  className="px-3 py-1.5 rounded-lg text-sm font-semibold transition-colors bg-red-600 text-white hover:bg-red-700"
                >
                  退出
                </a>
              </div>
              
              {/* Controls */}
              <div className="flex items-center gap-3">
                <a
                  href={`${APP_BASE}/config`}
                  className="px-3 py-1.5 rounded-lg text-sm font-semibold transition-colors bg-blue-600 text-white hover:bg-blue-700"
                >
                  查看配置
                </a>
                <span className={`text-sm ${isDarkMode ? 'text-gray-300' : 'text-gray-600'}`}>
                  {isDarkMode ? '🌙 Dark' : '☀️ Light'}
                </span>
                <button
                  onClick={() => setIsDarkMode(!isDarkMode)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                    isDarkMode ? 'bg-purple-600' : 'bg-gray-300'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      isDarkMode ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>
            </div>

            {/* Selected Path Display */}
            {selectedPath && (
              <div className={`p-3 ${isDarkMode ? 'bg-gray-700 border-gray-600' : 'bg-blue-50 border-blue-200'} border rounded-lg`}>
                <p className={`text-xs font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-600'} mb-1`}>已选择路径：</p>
                <p className={`text-sm ${isDarkMode ? 'text-white' : 'text-gray-800'} break-all font-mono`}>{selectedPath}</p>
              </div>
            )}

            {/* Arguments */}
            <div className="space-y-2">
              <label className={`text-sm font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-700'}`}>Additional Arguments:</label>
              <input
                type="text"
                value={customArgs}
                onChange={(e) => setCustomArgs(e.target.value)}
                placeholder="--tmdb movie/12345 --trackers ptp,aither,ulcx --no-edition --no-tag"
                className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                  isDarkMode 
                    ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400' 
                    : 'bg-white border-gray-300 text-gray-900'
                }`}
                disabled={isExecuting}
              />
            </div>
            
            {/* Description Link URL Input - shown when --desclink is in args */}
            {/* Hide when valid URL and not focused; show when empty, focused, or invalid */}
            {hasDescLink && (!descLinkUrl || descLinkFocused || descLinkError) && (
              <div className="space-y-2">
                <label className={`text-sm font-semibold ${isDarkMode ? 'text-gray-300' : 'text-gray-700'} flex items-center gap-2`}>
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                  </svg>
                  Description Link URL (pastebin, hastebin, etc.):
                </label>
                <input
                  type="url"
                  value={descLinkUrl}
                  onChange={(e) => updateDescLink(e.target.value)}
                  onFocus={() => setDescLinkFocused(true)}
                  onBlur={() => setDescLinkFocused(false)}
                  placeholder="https://pastebin.com/abc123"
                  className={`w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                    descLinkError 
                      ? 'border-red-500 focus:ring-red-500' 
                      : isDarkMode 
                        ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400' 
                        : 'bg-white border-gray-300 text-gray-900'
                  }`}
                  disabled={isExecuting}
                />
                {descLinkError && (
                  <p className="text-xs text-red-500 mt-1">{descLinkError}</p>
                )}
                {descLinkUrl && !descLinkError && (
                  <p className="text-xs text-green-500 mt-1">Valid paste URL</p>
                )}
              </div>
            )}
            
            {/* Description File Status - only show on error or when no file selected */}
            {hasDescFile && (descFileError || !descFilePath) && (
              <div className={`p-3 rounded-lg ${
                descFileError 
                  ? isDarkMode ? 'bg-red-900 border border-red-700' : 'bg-red-50 border border-red-200'
                  : isDarkMode ? 'bg-yellow-900 border border-yellow-700' : 'bg-yellow-50 border border-yellow-200'
              }`}>
                <div className="flex items-center gap-2">
                  <svg className={`w-4 h-4 ${
                    descFileError ? 'text-red-500' : 'text-yellow-500'
                  }`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    {descFileError ? (
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    ) : (
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    )}
                  </svg>
                  <span className={`text-sm font-medium ${
                    descFileError 
                      ? isDarkMode ? 'text-red-300' : 'text-red-700'
                      : isDarkMode ? 'text-yellow-300' : 'text-yellow-700'
                  }`}>
                    {descFileError 
                      ? 'Invalid description file path' 
                      : 'Select a description file from the left panel or enter a path'}
                  </span>
                </div>
                {descFilePath && descFileError && (
                  <p className={`text-xs mt-1 break-all font-mono ${isDarkMode ? 'text-red-400' : 'text-red-600'}`}>
                    {descFilePath}
                  </p>
                )}
                {descFileError && (
                  <p className={`text-xs mt-1 ${isDarkMode ? 'text-red-400' : 'text-red-600'}`}>
                    {descFileError}
                  </p>
                )}
              </div>
            )}

            {/* Execute Button */}
            <div className="flex gap-2">
              <button
                onClick={executeCommand}
                disabled={!selectedPath || isExecuting}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:bg-gray-400 disabled:cursor-not-allowed transition-colors font-medium text-lg"
              >
                <PlayIcon />
                {isExecuting ? 'Executing...' : 'Execute Upload'}
              </button>
              <button
                onClick={clearTerminal}
                className={`flex items-center gap-2 px-4 py-3 rounded-lg transition-colors ${
                  isExecuting 
                    ? 'bg-red-600 hover:bg-red-700 text-white' 
                    : 'bg-gray-600 hover:bg-gray-700 text-white'
                }`}
                title={isExecuting ? 'Kill process and clear terminal' : 'Clear terminal'}
              >
                <TrashIcon />
                {isExecuting ? 'Kill & Clear' : 'Clear'}
              </button>
            </div>
          </div>
        </div>

        {/* Execution Output */}
        <div className={`flex-1 ${isDarkMode ? 'bg-gray-900' : 'bg-gray-100'} p-4 flex flex-col min-h-0 overflow-hidden`}>
          <div className="max-w-6xl mx-auto w-full flex-1 flex flex-col min-h-0">
            <div className="flex items-center gap-2 mb-3 flex-shrink-0">
              <span className={isDarkMode ? 'text-white' : 'text-gray-800'}><TerminalIcon /></span>
              <h3 className={`text-lg font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'}`}>Execution Output</h3>
              {isExecuting && (
                <span className="ml-auto text-sm text-green-400 animate-pulse">● Running</span>
              )}
            </div>

            {/* Rich HTML output (rendered from Rich export_html fragments) */}
            <div
              ref={richOutputRef}
              id="rich-output"
              className={`flex-1 rounded-lg overflow-auto p-3 border ${isDarkMode ? 'bg-gray-900 border-gray-700 text-white' : 'bg-white border-gray-200 text-gray-900'}`}
            ></div>
            {isExecuting && (
              <div className="mt-2 flex gap-2">
                <input
                  ref={inputRef}
                  value={userInput}
                  onChange={(e) => setUserInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); sendInput(sessionId, userInput); } }}
                  placeholder="Type input and press Enter"
                  className={`flex-1 px-3 py-2 rounded-lg border focus:ring-2 focus:ring-purple-500 focus:border-transparent ${isDarkMode ? 'bg-gray-700 border-gray-600 text-white' : 'bg-white border-gray-300 text-gray-900'}`}
                />
                <button
                  onClick={() => sendInput(sessionId, userInput)}
                  disabled={!sessionId || !userInput}
                  className="px-4 py-2 rounded-lg bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
                >
                  Send
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      {/* Right Resize Handle */}
      <div
        className={`w-1 ${isDarkMode ? 'bg-gray-700 hover:bg-purple-500' : 'bg-gray-300 hover:bg-purple-500'} cursor-col-resize transition-colors`}
        onMouseDown={startResizingRight}
        style={{ userSelect: 'none' }}
      />

      {/* Right Sidebar - Arguments */}
      <div
        className={`${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-200'} border-l flex flex-col`}
        style={{ width: `${rightSidebarWidth}px`, minWidth: '200px', maxWidth: '800px' }}
      >
        <div className={`p-4 border-b ${isDarkMode ? 'border-gray-700 bg-gray-900' : 'border-gray-200 bg-gradient-to-l from-purple-50 to-blue-50'}`}>
          <h2 className={`text-lg font-bold ${isDarkMode ? 'text-white' : 'text-gray-800'} flex items-center gap-2`}>
            <TerminalIcon />
            Arguments
          </h2>
        </div>
        
        {/* Search and Collapse Controls */}
        <div className={`p-3 border-b ${isDarkMode ? 'border-gray-700' : 'border-gray-200'} space-y-2`}>
          {/* Search Input */}
          <div className="relative">
            <div className={`absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
              <SearchIcon />
            </div>
            <input
              type="text"
              value={argSearchFilter}
              onChange={(e) => setArgSearchFilter(e.target.value)}
              placeholder="Search arguments..."
              className={`w-full pl-10 pr-3 py-2 text-sm border rounded-lg focus:ring-2 focus:ring-purple-500 focus:border-transparent ${
                isDarkMode 
                  ? 'bg-gray-700 border-gray-600 text-white placeholder-gray-400' 
                  : 'bg-white border-gray-300 text-gray-900 placeholder-gray-500'
              }`}
            />
            {argSearchFilter && (
              <button
                onClick={() => setArgSearchFilter('')}
                className={`absolute inset-y-0 right-0 pr-3 flex items-center ${isDarkMode ? 'text-gray-400 hover:text-gray-200' : 'text-gray-500 hover:text-gray-700'}`}
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
          
          {/* Collapse/Expand All Buttons */}
          <div className="flex gap-2">
            <button
              onClick={collapseAllSections}
              className={`flex-1 flex items-center justify-center gap-1 px-2 py-1.5 text-xs font-medium rounded-md border transition-colors ${
                isDarkMode 
                  ? 'bg-gray-700 border-gray-600 text-gray-300 hover:bg-gray-600' 
                  : 'bg-gray-100 border-gray-300 text-gray-600 hover:bg-gray-200'
              }`}
            >
              <CollapseAllIcon />
              Collapse All
            </button>
            <button
              onClick={expandAllSections}
              className={`flex-1 flex items-center justify-center gap-1 px-2 py-1.5 text-xs font-medium rounded-md border transition-colors ${
                isDarkMode 
                  ? 'bg-gray-700 border-gray-600 text-gray-300 hover:bg-gray-600' 
                  : 'bg-gray-100 border-gray-300 text-gray-600 hover:bg-gray-200'
              }`}
            >
              <ExpandAllIcon />
              Expand All
            </button>
          </div>
        </div>
        
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {filteredCategories.length === 0 ? (
            <div className={`text-center py-8 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>
              <p className="text-sm">No arguments found matching "{argSearchFilter}"</p>
            </div>
          ) : (
            filteredCategories.map((cat) => (
              <div key={cat.title} className={`rounded-lg border ${isDarkMode ? 'border-gray-700' : 'border-gray-200'}`}>
                {/* Collapsible Section Header */}
                <button
                  onClick={() => toggleSectionCollapse(cat.title)}
                  className={`w-full flex items-center justify-between p-3 text-left transition-colors rounded-t-lg ${
                    isDarkMode 
                      ? 'hover:bg-gray-700' 
                      : 'hover:bg-gray-50'
                  } ${collapsedSections.has(cat.title) ? 'rounded-b-lg' : ''}`}
                >
                  <div className="flex-1">
                    <div className={`text-sm font-bold ${isDarkMode ? 'text-gray-100' : 'text-gray-900'} flex items-center gap-2`}>
                      <span className={isDarkMode ? 'text-gray-400' : 'text-gray-500'}>
                        {collapsedSections.has(cat.title) ? <ChevronRightIcon /> : <ChevronDownIcon />}
                      </span>
                      {cat.title}
                      <span className={`text-xs font-normal px-1.5 py-0.5 rounded ${isDarkMode ? 'bg-gray-700 text-gray-400' : 'bg-gray-200 text-gray-500'}`}>
                        {cat.args.length}
                      </span>
                    </div>
                    {cat.subtitle && !collapsedSections.has(cat.title) && (
                      <div className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>{cat.subtitle}</div>
                    )}
                  </div>
                </button>
                
                {/* Collapsible Section Content */}
                {!collapsedSections.has(cat.title) && (
                  <div className={`px-3 pb-3 pt-2 ${isDarkMode ? 'border-t border-gray-700' : 'border-t border-gray-200'}`}>
                    <div className="grid grid-cols-[repeat(auto-fit,minmax(250px,1fr))] gap-2">
                      {cat.args.map((a) => (
                        <div
                          key={a.label}
                          className={`w-full p-2 rounded-lg border ${isDarkMode ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-100'}`}
                        >
                          <div className="flex items-start justify-between gap-3">
                            <button
                              onClick={() => addArgument(a.label)}
                              disabled={isExecuting}
                              className={`px-3 py-1 text-sm font-mono rounded-md border ${isDarkMode ? 'bg-gray-700 border-gray-600 text-white hover:bg-purple-600 hover:text-white' : 'bg-white border-gray-200 text-gray-800 hover:bg-purple-600 hover:text-white'} transition-colors`}
                            >
                              {a.label}
                            </button>
                            <div className="flex-1 text-right">
                              {a.placeholder && (
                                <div className={`text-xs ${isDarkMode ? 'text-gray-300' : 'text-gray-500'} font-mono`}>{a.placeholder}</div>
                              )}
                            </div>
                          </div>
                          {a.description && (
                            <div className={`text-xs mt-1 ${isDarkMode ? 'text-gray-400' : 'text-gray-500'}`}>{a.description}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// Render the app
const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<AudionutsUAGUI />);
