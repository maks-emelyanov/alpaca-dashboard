var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};

dagfuncs.dashboardMoney = function (value) {
    if (value === null || value === undefined || value === '') return '—';
    return new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 2}).format(value);
};
dagfuncs.dashboardNumber = function (value) {
    if (value === null || value === undefined || value === '') return '—';
    return new Intl.NumberFormat('en-US', {maximumFractionDigits: 8}).format(value);
};
dagfuncs.dashboardPercent = function (value) {
    if (value === null || value === undefined || value === '') return '—';
    return (value > 0 ? '+' : '') + new Intl.NumberFormat('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(value) + '%';
};
dagfuncs.dashboardDuration = function (value) {
    if (value === null || value === undefined || value === '') return '—';
    const seconds = Math.max(0, Math.floor(Number(value)));
    if (seconds >= 86400) return Math.floor(seconds / 86400) + 'd ' + Math.floor((seconds % 86400) / 3600) + 'h';
    if (seconds >= 3600) return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    if (seconds >= 60) return Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's';
    return seconds + 's';
};
dagfuncs.dashboardDate = function (value) {
    if (!value) return '—';
    const stamp = new Date(value);
    if (Number.isNaN(stamp.getTime())) return value;
    return new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'}).format(stamp);
};

// A holding can have several active exit orders at different prices.
dagfuncs.dashboardPrices = function (value) {
    if (value === null || value === undefined || value === '') return '—';
    return String(value).split(',').map(price => dagfuncs.dashboardMoney(Number(price.trim()))).join(' / ');
};

// Theme API ships its styles with AG Grid; no CDN or legacy CSS is needed.
dagfuncs.dashboardTheme = function (theme) {
    return theme.withParams({
        backgroundColor: '#121b15', foregroundColor: '#d4e0d7', accentColor: '#35d498',
        headerBackgroundColor: '#1b261e', headerTextColor: '#90a798',
        oddRowBackgroundColor: '#151f18', borderColor: '#2b3a2e',
        selectedRowBackgroundColor: '#253f2e', browserColorScheme: 'dark',
        fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: 12,
        headerHeight: 42, rowHeight: 44, wrapperBorderRadius: 8
    });
};
