(function () {
  const en = {
    // Main UI
    'Upload Assistant': 'Upload Assistant',
    'Upload Assistant Web UI': 'Upload Assistant Web UI',
    'Upload Assistant Interactive Output': 'Upload Assistant Interactive Output',
    'Files': 'Files',
    'Upload': 'Upload',
    'Arguments': 'Arguments',
    'File Browser': 'File Browser',
    'Search files and folders...': 'Search files and folders...',
    'Searching...': 'Searching...',
    'Selected Path:': 'Selected Path:',
    'Execute Upload': 'Execute Upload',
    'Executing...': 'Executing...',
    'Output': 'Output',
    'Config': 'Config',
    'Logout': 'Logout',
    'View Config': 'View Config',
    'Quick Start Help':
      '\nQuick Start:\n  1. Select a file or folder from the left panel\n  2. Add Upload Assistant arguments (optional)\n  3. Click "Execute Upload" to start\n',

    // Config UI
    'Upload Assistant Config': 'Upload Assistant Config',
    'Save Config': 'Save Config',
    'Saving...': 'Saving...',
    'Back to Upload': 'Back to Upload',
    'Security': 'Security',
    'Access Log': 'Access Log',
    'Access Log Settings': 'Access Log Settings',
    'Control what the Web UI logs. Default is access_denied (only failed API attempts). Choose disabled to turn off all logging.':
      'Control what the Web UI logs. Default is access_denied (only failed API attempts). Choose disabled to turn off all logging.',
    'Level': 'Level',
    'Save': 'Save',
    'Recent Access Log': 'Recent Access Log',
    'Loading...': 'Loading...',
    'Loading log entries...': 'Loading log entries...',
    'No log entries found.': 'No log entries found.',
    'Refresh': 'Refresh',
    'Saved.': 'Saved.',
    'Save IP Settings': 'Save IP Settings',
    'Scan the QR code with your authenticator app, then enter the 6-digit code below.':
      'Scan the QR code with your authenticator app, then enter the 6-digit code below.',
    'Please enter a valid 6-digit code': 'Please enter a valid 6-digit code',
  };

  if (typeof window !== 'undefined') {
    window.UALocales = window.UALocales || {};
    window.UALocales.en = { ...(window.UALocales.en || {}), ...en };
  }
})();

