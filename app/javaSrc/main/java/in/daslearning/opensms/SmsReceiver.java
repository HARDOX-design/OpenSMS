package in.daslearning.opensms;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.provider.Telephony;
import android.telephony.SmsMessage;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

/**
 * Persists inbound SMS for the foreground service to deliver as a webhook.
 *
 * The receiver never performs network I/O: it only appends one JSON line to
 * files/config/inbox.jsonl, which services/appSvc.py drains atomically.
 */
public class SmsReceiver extends BroadcastReceiver {

    private static final String TAG = "OpenSMS";
    private static final String INBOX_DIR = "inbox";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null ||
            !Telephony.Sms.Intents.SMS_RECEIVED_ACTION.equals(intent.getAction())) {
            return;
        }

        long receivedAt = System.currentTimeMillis();
        try {
            SmsMessage[] messages =
                Telephony.Sms.Intents.getMessagesFromIntent(intent);
            if (messages == null || messages.length == 0) {
                return;
            }

            StringBuilder body = new StringBuilder();
            String sender = null;
            long sentAt = 0L;
            for (SmsMessage message : messages) {
                if (message == null) {
                    continue;
                }
                if (sender == null) {
                    sender = message.getOriginatingAddress();
                }
                if (message.getTimestampMillis() > sentAt) {
                    sentAt = message.getTimestampMillis();
                }
                String part = message.getMessageBody();
                if (part != null) {
                    body.append(part);
                }
            }

            JSONObject record = new JSONObject();
            record.put("from", sender == null ? "unknown" : sender);
            record.put("text", body.toString());
            record.put("received_at_ms", receivedAt);
            record.put("sent_at_ms", sentAt);
            record.put("parts", messages.length);

            int subscriptionId = intent.getIntExtra("subscription", -1);
            if (subscriptionId >= 0) {
                record.put("subscription_id", subscriptionId);
            }
            int slot = intent.getIntExtra("slot", -1);
            if (slot >= 0) {
                record.put("slot", slot);
            }

            File inboxDir = new File(context.getFilesDir(), "config/" + INBOX_DIR);
            if (!inboxDir.exists() && !inboxDir.mkdirs()) {
                Log.e(TAG, "Could not create the inbox directory");
                return;
            }

            // Write to a .part file, then rename: the service only claims
            // finished .json files, so it never reads a partial record.
            File tempFile = File.createTempFile("inbound-", ".part", inboxDir);
            try (FileOutputStream out = new FileOutputStream(tempFile)) {
                out.write((record.toString() + "\n")
                    .getBytes(StandardCharsets.UTF_8));
                out.flush();
            }

            File finalFile = new File(
                inboxDir,
                "sms-" + receivedAt + "-" + UUID.randomUUID() + ".json"
            );
            if (!tempFile.renameTo(finalFile)) {
                Log.e(TAG, "Could not move the inbound SMS into the inbox");
                tempFile.delete();
            }
        } catch (Exception exc) {
            Log.e(TAG, "Could not persist the inbound SMS", exc);
        }
    }
}
