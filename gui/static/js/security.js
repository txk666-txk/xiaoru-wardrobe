// Bootstrap once, then attach CSRF only to requests to this workbench origin.
(function () {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const ready = nativeFetch('/api/security/session', { credentials: 'same-origin', cache: 'no-store' })
    .then(async response => {
      if (!response.ok) throw new Error('工作台会话初始化失败，请重新登录或刷新页面');
      return (await response.json()).csrf;
    });
  // Keep early initialization failures observable without an unhandled rejection.
  ready.catch(error => console.error(error.message));
  window.fetch = async function (input, options) {
    const request = new Request(input, options);
    if (new URL(request.url).origin !== window.location.origin) return nativeFetch(request);
    const csrf = await ready;
    const headers = new Headers(request.headers);
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method)) headers.set('X-Fay-CSRF', csrf);
    return nativeFetch(new Request(request, { headers, credentials: 'same-origin' }));
  };
  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, async = true, ...rest) {
    this.fayNeedsCSRF = new URL(url, window.location.href).origin === window.location.origin &&
      !['GET', 'HEAD', 'OPTIONS'].includes(String(method).toUpperCase());
    this.faySynchronous = async === false;
    return nativeOpen.call(this, method, url, async, ...rest);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (!this.fayNeedsCSRF) return nativeSend.call(this, body);
    if (this.faySynchronous) throw new Error('工作台不支持同步写请求');
    ready.then(csrf => {
      if (this.readyState !== 1) return;
      this.setRequestHeader('X-Fay-CSRF', csrf);
      nativeSend.call(this, body);
    }).catch(error => {
      console.error(error);
      this.dispatchEvent(new Event('error'));
    });
  };
  window.faySecurity = {
    ready,
    async ticket(username, audience) {
      const response = await window.fetch('/api/security/ws-ticket', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, audience })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || '无法验证聊天连接');
      return data.ticket;
    }
  };
}());
