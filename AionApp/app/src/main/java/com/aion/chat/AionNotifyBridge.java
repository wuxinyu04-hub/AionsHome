package com.aion.chat;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import android.Manifest;
import android.content.pm.PackageManager;
import android.webkit.JavascriptInterface;

/**
 * 网页 JS 通过 window.AionNotify.show(title, text) 发一条系统通知。
 * 用途：小米运动健康 App 的通知转发会监听系统通知并推送到手环（震动+显示），
 * 这样 AI 的消息就能落到手环上。
 * 链路：后端 WS → chat.js → AionNotify.show() → NotificationManager → 小米运动健康 → 手环。
 */
public final class AionNotifyBridge {
    private static final String CHANNEL_ID = "aion_band_notify";
    private static final int NOTIFY_ID = 9001;

    private final Context context;

    public AionNotifyBridge(Context context) {
        this.context = context.getApplicationContext();
        ensureChannel();
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null && nm.getNotificationChannel(CHANNEL_ID) == null) {
                NotificationChannel ch = new NotificationChannel(
                        CHANNEL_ID,
                        "Aion 手环通知",
                        NotificationManager.IMPORTANCE_HIGH);
                ch.setDescription("AI 消息推送到手环（经小米运动健康转发）");
                ch.enableVibration(true);
                nm.createNotificationChannel(ch);
            }
        }
    }

    @JavascriptInterface
    public boolean show(String title, String text) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        NotificationCompat.Builder b = new NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_email)
                .setContentTitle(title == null ? "Aion" : title)
                .setContentText(text == null ? "" : text)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setAutoCancel(true);
        try {
            NotificationManagerCompat.from(context).notify(NOTIFY_ID, b.build());
            return true;
        } catch (SecurityException ignored) {
            return false;
        }
    }

    @JavascriptInterface
    public void cancel() {
        try {
            NotificationManagerCompat.from(context).cancel(NOTIFY_ID);
        } catch (SecurityException ignored) {}
    }
}
