package com.aion.chat;

/**
 * 屏幕活动策略：去抖 + 熄屏存活心跳时间决策。
 * 纯 Java 无 Android 框架依赖，可单测。时间参数用 SystemClock.elapsedRealtime() 语义，
 * 避免墙钟跳变产生突发上报。
 */
final class ScreenActivityPolicy {

    enum Transition { NONE, SCREEN_ON, SCREEN_OFF }

    private volatile boolean screenOn = true;
    private volatile long lastAliveEmitAt = 0;

    /**
     * 观测一次屏幕状态（由广播接收器调用）。
     * @param nowOn 当前是否亮屏
     * @param nowElapsed elapsedRealtime() 语义的时间戳
     * @return NONE 表示状态无变化（重复广播），SCREEN_ON / SCREEN_OFF 表示真实转换
     */
    Transition onObserved(boolean nowOn, long nowElapsed) {
        if (nowOn == screenOn) return Transition.NONE;
        screenOn = nowOn;
        if (nowOn) {
            lastAliveEmitAt = 0;  // 回到亮屏，熄灭存活心跳
        }
        return nowOn ? Transition.SCREEN_ON : Transition.SCREEN_OFF;
    }

    /**
     * 熄屏期间，距上次发射存活心跳是否已超过 intervalMs。
     */
    boolean shouldEmitAliveHeartbeat(long nowElapsed, long intervalMs) {
        return !screenOn && nowElapsed - lastAliveEmitAt >= intervalMs;
    }

    /**
     * 发射存活心跳后调用，重置计时起点。
     */
    void resetHeartbeatDeadline(long nowElapsed) {
        lastAliveEmitAt = nowElapsed;
    }

    boolean isScreenOn() {
        return screenOn;
    }

    /**
     * 服务启动时从真实屏幕状态初始化（广播不可粘滞，服务可能恰好在熄屏时重启）。
     */
    void init(boolean realScreenOn, long nowElapsed) {
        screenOn = realScreenOn;
        lastAliveEmitAt = realScreenOn ? 0 : nowElapsed;
    }
}