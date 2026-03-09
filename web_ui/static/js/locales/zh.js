(function () {
  const zh = {
    // 主界面
    'Upload Assistant': '发种助手',
    'Upload Assistant Web UI': '发种助手 Web UI',
    'Upload Assistant Interactive Output': '发种助手 交互输出',
    'Files': '文件',
    'Upload': '上传',
    'Arguments': '参数',
    'File Browser': '文件浏览器',
    'Search files and folders...': '搜索文件和文件夹...',
    'Searching...': '搜索中...',
    'Selected Path:': '已选择路径：',
    'Execute Upload': '开始上传',
    'Executing...': '执行中...',
    'Output': '输出',
    'Config': '配置',
    'Logout': '退出',
    'View Config': '查看配置',
    'Quick Start Help':
      '\n快速开始：\n  1. 在左侧选择一个文件或文件夹\n  2. 在右侧（可选）添加命令行参数\n  3. 点击下方“开始上传”按钮\n',

    // 配置界面
    'Upload Assistant Config': '发种助手 配置',
    'Save Config': '保存配置',
    'Saving...': '保存中...',
    'Back to Upload': '返回发种页面',
    'Security': '安全设置',
    'Access Log': '访问日志',
    'Access Log Settings': '访问日志设置',
    'Control what the Web UI logs. Default is access_denied (only failed API attempts). Choose disabled to turn off all logging.':
      '控制 Web UI 记录哪些日志。默认 access_denied 仅记录失败的 API 请求，选择 disabled 则完全关闭日志。',
    'Level': '级别',
    'Save': '保存',
    'Recent Access Log': '最近访问日志',
    'Loading...': '加载中...',
    'Loading log entries...': '正在加载日志...',
    'No log entries found.': '暂无日志记录。',
    'Refresh': '刷新',
    'Saved.': '已保存。',
    'Save IP Settings': '保存 IP 设置',
    'Scan the QR code with your authenticator app, then enter the 6-digit code below.':
      '请使用认证器 App 扫描二维码，然后在下方输入 6 位验证码。',
    'Please enter a valid 6-digit code': '请输入有效的 6 位验证码',
  };

  if (typeof window !== 'undefined') {
    window.UALocales = window.UALocales || {};
    window.UALocales.zh = { ...(window.UALocales.zh || {}), ...zh };
    window.UALocales['zh-CN'] = window.UALocales.zh;
  }
})();

