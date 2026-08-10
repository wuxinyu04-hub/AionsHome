package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.*;

public class ScreenActivityPolicyTest {

    private final ScreenActivityPolicy p = new ScreenActivityPolicy();

    @Test
    public void initialOffSuppressesHeartbeat() {
        // 默认 screenOn=true，不应发射存活心跳
        assertFalse(p.shouldEmitAliveHeartbeat(0, 600_000));
    }

    @Test
    public void firstOffEmitsTransition() {
        assertEquals(ScreenActivityPolicy.Transition.SCREEN_OFF, p.onObserved(false, 100));
        // 熄屏后，deadline 在 100+10min，nowElapsed=10min 应发射
        assertTrue(p.shouldEmitAliveHeartbeat(10 * 60_000, 600_000));
    }

    @Test
    public void duplicateOffIsNone() {
        p.onObserved(false, 100);
        assertEquals(ScreenActivityPolicy.Transition.NONE, p.onObserved(false, 200));
    }

    @Test
    public void onAfterOffEmitsOn() {
        p.onObserved(false, 100);
        assertEquals(ScreenActivityPolicy.Transition.SCREEN_ON, p.onObserved(true, 200));
    }

    @Test
    public void duplicateOnIsNone() {
        p.onObserved(true, 100);
        assertEquals(ScreenActivityPolicy.Transition.NONE, p.onObserved(true, 200));
    }

    @Test
    public void heartbeatSuppressedBeforeInterval() {
        p.onObserved(false, 100);
        // 还没到 deadline
        assertFalse(p.shouldEmitAliveHeartbeat(100, 600_000));
    }

    @Test
    public void heartbeatEmittedAtInterval() {
        p.onObserved(false, 100);
        assertTrue(p.shouldEmitAliveHeartbeat(10 * 60_000 + 1, 600_000));
    }

    @Test
    public void repeatedHeartbeatCheckSameEpoch() {
        p.onObserved(false, 100);
        // 第一次检查：到时间了
        assertTrue(p.shouldEmitAliveHeartbeat(10 * 60_000 + 1, 600_000));
        // 发射后 reset，再检查：不应再发射
        p.resetHeartbeatDeadline(10 * 60_000 + 1);
        assertFalse(p.shouldEmitAliveHeartbeat(10 * 60_000 + 1, 600_000));
    }

    @Test
    public void onTransitionResetsHeartbeat() {
        p.onObserved(false, 100);
        p.onObserved(true, 200);
        // 回到亮屏后不应发射存活心跳
        assertFalse(p.shouldEmitAliveHeartbeat(10 * 60_000 + 1, 600_000));
    }

    @Test
    public void reenteringOffResetsDeadline() {
        p.onObserved(false, 100);
        p.onObserved(true, 200);
        p.onObserved(false, 500);
        // 第二次熄屏，deadline 应重置到 500+10min
        assertFalse(p.shouldEmitAliveHeartbeat(500, 600_000));
        assertTrue(p.shouldEmitAliveHeartbeat(10 * 60_000 + 500, 600_000));
    }

    @Test
    public void initOffSetsHeartbeatDeadline() {
        p.init(false, 0);
        assertFalse(p.isScreenOn());
        assertTrue(p.shouldEmitAliveHeartbeat(10 * 60_000 + 1, 600_000));
    }

    @Test
    public void initOnNoHeartbeat() {
        p.init(true, 0);
        assertTrue(p.isScreenOn());
        assertFalse(p.shouldEmitAliveHeartbeat(10 * 60_000, 600_000));
    }
}