/* ── 软键盘避让 ──
   .input-area 是 position:fixed + bottom，钉在 layout viewport 底部。
   - 手机浏览器/PWA：键盘弹起 → visualViewport 缩 → 算出 kbd → --kbd 抬起。
   - Android APP(edge-to-edge)：窗口不缩、vv 不变，原生 Type.ime() 测高度
     通过 aion-ime-insets 事件 + AionKbd.getImeHeight() 桥喂进来。
   两条路取大值写进 --kbd，谁先到谁生效，互不干扰。 */
(function () {
  const vv = window.visualViewport;
  const root = document.documentElement;
  let pending = false;
  let src = '-';        // 'native' / 'vv' / 'inner' —— 当前 --kbd 由谁提供
  let nativeKbd = -1;    // 原生喂的高度；-1 表示还没收到事件，用桥轮询

  function applyKbd() {
    pending = false;
    // 轮询原生桥（兜底：insets 变化时事件应已 dispatch，但冷启动咬合慢）
    if (nativeKbd < 0 && window.AionKbd && typeof window.AionKbd.getImeHeight === 'function') {
      try { nativeKbd = window.AionKbd.getImeHeight() || 0; } catch (e) {}
    }
    let kbd = 0; src = '-';
    let kNative = Math.max(0, nativeKbd);
    // 原生路径优先（APP 内权威）；nativeKbd=0 可能只是还没弹过，不压 vv/inner
    if (kNative > 40) { kbd = kNative; src = 'native'; }

    let kVV = 0;
    if (vv) {
      kVV = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      if (kbd === 0 && kVV > 40) { kbd = kVV; src = 'vv'; }
    }
    if (kbd === 0) {
      const shrink = Math.max(0, (window.__kbdBaseH || window.innerHeight) - window.innerHeight);
      if (shrink > 40) { kbd = shrink; src = 'inner'; }
    }
    root.style.setProperty('--kbd', (kbd > 40 ? kbd : 0) + 'px');
    root.style.setProperty('--kbd-src', src);
  }

  function schedule() { if (pending) return; pending = true; requestAnimationFrame(applyKbd); }

  // 原生事件：Java 端 pushImeHeightToJs dispatch 的 aion-ime-insets
  window.addEventListener('aion-ime-insets', function (e) {
    const h = e && e.detail ? (e.detail.height | 0) : 0;
    nativeKbd = Math.max(0, h);
    schedule();
  });

  // 进页面立刻拉一次原生高度（键盘已弹起时冷启动复用）
  if (window.AionKbd && typeof window.AionKbd.getImeHeight === 'function') {
    try { nativeKbd = window.AionKbd.getImeHeight() || 0; } catch (e) {}
  }
  // 原生事件没来前每 500ms 轮询一次，最多 8 秒（冷启动 insets 慢就绪）
  let pollN = 0;
  const pollTimer = setInterval(function () {
    pollN++;
    if (window.AionKbd && typeof window.AionKbd.getImeHeight === 'function') {
      try { nativeKbd = window.AionKbd.getImeHeight() || 0; } catch (e) {}
    }
    if (pollN >= 16 || nativeKbd > 40) { clearInterval(pollTimer); if (nativeKbd > 40) schedule(); }
  }, 500);

  if (vv) { vv.addEventListener('resize', schedule); vv.addEventListener('scroll', schedule); }
  window.addEventListener('resize', schedule);
  window.addEventListener('orientationchange', () => setTimeout(schedule, 200));

  setTimeout(function () { if (!window.__kbdBaseH) window.__kbdBaseH = window.innerHeight; }, 600);

  applyKbd();
})();
