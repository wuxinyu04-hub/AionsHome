package com.aion.chat.miband;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * BLE 手环历史上传失败时的落盘待传队列。
 *
 * syncNow 从手环读出的历史样本若上传失败（服务端 401/网络抖动），不再直接丢弃：
 * 序列化到本地文件，下次同步先重试队列再拉新数据。游标仍前进（避免无限重拉
 * BLE 历史，BLE 历史不可重放且手环持续产数据会让每批越来越大），但样本不丢。
 *
 * 线程安全由 MiBandRuntime.operationExecutor（单线程）保证，本类不做内部加锁。
 */
public final class MiBandUploadOutbox {
    private static final int MAX_BATCHES = 20;
    private final File file;

    public MiBandUploadOutbox(Context context) {
        File dir = new File(context.getFilesDir(), "mi_band_outbox");
        if (!dir.exists()) dir.mkdirs();
        this.file = new File(dir, "pending_batches.json");
    }

    public boolean hasPending() {
        return file.exists() && file.length() > 0;
    }

    /** 追加一批失败样本；超过 MAX_BATCHES 丢弃最旧，避免无限增长。 */
    public void appendBatch(String deviceName, List<MiBandProtocol.ActivitySample> samples) throws org.json.JSONException {
        if (samples == null || samples.isEmpty()) return;
        JSONArray batches = readBatches();
        batches.put(batchToJson(deviceName, samples));
        while (batches.length() > MAX_BATCHES) {
            batches.remove(0);
        }
        writeBatches(batches);
    }

    /** 逐批重试上传；成功的删除，失败的保留。返回是否全部清空。 */
    public boolean drain(MiBandRuntime.SampleSink sink) throws Exception {
        if (!hasPending()) return true;
        JSONArray batches = readBatches();
        if (batches.length() == 0) return true;
        JSONArray remaining = new JSONArray();
        for (int i = 0; i < batches.length(); i++) {
            JSONObject batch = batches.optJSONObject(i);
            if (batch == null) continue;
            try {
                sink.upload(batch.optString("device_name", ""), samplesFromJson(batch));
            } catch (Exception failure) {
                batch.put("_last_error", safeMessage(failure));
                remaining.put(batch);
            }
        }
        writeBatches(remaining);
        return remaining.length() == 0;
    }

    private JSONArray readBatches() {
        try (FileInputStream fis = new FileInputStream(file)) {
            byte[] data = new byte[(int) file.length()];
            int read = fis.read(data);
            if (read <= 0) return new JSONArray();
            String content = new String(data, 0, read, StandardCharsets.UTF_8);
            if (content.isEmpty()) return new JSONArray();
            JSONObject root = new JSONObject(content);
            JSONArray arr = root.optJSONArray("batches");
            return arr != null ? arr : new JSONArray();
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private void writeBatches(JSONArray batches) {
        try (FileOutputStream fos = new FileOutputStream(file)) {
            JSONObject root = new JSONObject();
            root.put("batches", batches);
            fos.write(root.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            // 落盘失败只能放弃：内存批次随 sync 结束丢失，但游标已推进，
            // 下次同步从更新位置开始，不会无限重拉 BLE。
        }
    }

    private JSONObject batchToJson(String deviceName, List<MiBandProtocol.ActivitySample> samples) throws org.json.JSONException {
        JSONArray items = new JSONArray();
        for (MiBandProtocol.ActivitySample s : samples) {
            JSONObject item = new JSONObject();
            item.put("measured_at", s.timestampMillis / 1000.0);
            item.put("raw_kind", s.rawKind);
            item.put("intensity", s.intensity);
            item.put("steps", s.steps);
            item.put("heart_rate", s.heartRate);
            item.put("unknown", s.unknown);
            item.put("sleep", s.sleep);
            item.put("deep_sleep", s.deepSleep);
            item.put("rem_sleep", s.remSleep);
            if (s.sleepStage != null) item.put("sleep_stage", s.sleepStage);
            items.put(item);
        }
        JSONObject batch = new JSONObject();
        batch.put("device_name", deviceName == null ? "" : deviceName);
        batch.put("samples", items);
        return batch;
    }

    private List<MiBandProtocol.ActivitySample> samplesFromJson(JSONObject batch) {
        List<MiBandProtocol.ActivitySample> list = new ArrayList<>();
        JSONArray items = batch.optJSONArray("samples");
        if (items == null) return list;
        for (int i = 0; i < items.length(); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item == null) continue;
            list.add(new MiBandProtocol.ActivitySample(
                    (long) (item.optDouble("measured_at", 0) * 1000.0),
                    item.optInt("raw_kind", 0),
                    item.optInt("intensity", 0),
                    item.optInt("steps", 0),
                    item.optInt("heart_rate", 0),
                    item.optInt("unknown", 0),
                    item.optInt("sleep", 0),
                    item.optInt("deep_sleep", 0),
                    item.optInt("rem_sleep", 0),
                    item.optString("sleep_stage", null)
            ));
        }
        return list;
    }

    private static String safeMessage(Throwable t) {
        return t == null ? "" : (t.getMessage() == null ? t.getClass().getSimpleName() : t.getMessage());
    }
}
