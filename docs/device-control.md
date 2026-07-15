# デバイス制御インターフェース

QLM29Hの操作は、常駐する `device_control.py` が次のコマンドファイルを監視して実行します。

```text
/home/pi/.config/qlm29h/device-command.json
```

実行状態と結果は次のファイルに書き出されます。

```text
/home/pi/.config/qlm29h/device-status.json
```

同じ `request_id` は一度しか実行されません。コマンドを実行するたびに新しい一意な `request_id` を指定します。コマンドファイルとステータスファイルは、将来Web UIやSORACOM経由の遠隔操作へ接続するための共通インターフェースとして利用できます。

## 対応アクション

| action | 動作 |
|---|---|
| `transmission_start` | `payload-control.json` の `enabled` を `true` にする |
| `transmission_stop` | `payload-control.json` の `enabled` を `false` にし、新規生成と保留データ送信を停止する |
| `dr_alignment_start` | 送信デーモンを止め、DR走行アライメントを開始する |
| `dr_alignment_cancel` | 実行中のDRアライメントを中止する |

## DRアライメント開始

```json
{
  "version": 1,
  "request_id": "dr-alignment-20260715-001",
  "action": "dr_alignment_start",
  "parameters": {
    "timeout_sec": 900,
    "minimum_state": 2,
    "clear_existing": false,
    "hot_start_mode": "2",
    "save_on_complete": true,
    "keep_message_output": false
  }
}
```

開始すると制御サービスは以下を自動で行います。

1. 通常のNMEA送信デーモンが稼働中か確認する
2. シリアルポートの競合を避けるため送信デーモンを停止する
3. DRを有効化し、`PQTMDRCAL` の状態監視を開始する
4. 2 m/s以上で走行し、3～4回曲がる間、`CalState` を監視する
5. `minimum_state`へ到達したら `PQTMDRSAVE` で保存する
6. 送信デーモンを再開する

`clear_existing` は、受信機の取付位置や角度を変更した場合だけ `true` にします。通常のアライメント開始では `false` のままにします。

## ステータス

実行中の例です。

```json
{
  "version": 1,
  "request_id": "dr-alignment-20260715-001",
  "action": "dr_alignment_start",
  "status": "running",
  "updated_at": "2026-07-15T10:00:00+00:00",
  "calibration_state": 1,
  "navigation_type": 3,
  "sender_was_active": true
}
```

`calibration_state` は `0`が未校正、`1`が軽度校正、`2`が校正完了、`3`が高精度方位まで校正完了です。

## 中止

```json
{
  "version": 1,
  "request_id": "dr-alignment-cancel-20260715-001",
  "action": "dr_alignment_cancel",
  "parameters": {
    "target_request_id": "dr-alignment-20260715-001"
  }
}
```

中止後も、開始前に送信デーモンが稼働していた場合は自動的に再開します。

## 障害時の動作

- 不正なJSONや未知のアクションは実行せず、`device-status.json` を `invalid` にします。
- 制御サービスがアライメント中に再起動した場合、同じ要求を再実行しません。既存校正の再クリアを防ぎ、送信デーモンの復旧を試みて `failed` を記録します。
- アライメント失敗、タイムアウト、中止の場合も、開始前に動いていた送信デーモンを再開します。
- DRアライメント中は通常のNMEA送信を停止するため、その時間のペイロードは生成されません。

## systemdサービス

`qlm29h-device-control.service` を `/etc/systemd/system/` に配置し、有効化します。このサービスは通常の送信デーモンと同じ`pi`ユーザーで動作し、サービス停止・再開だけを`sudo -n systemctl`で実行します。そのため、実機では`pi`ユーザーが対象サービスをパスワードなしで停止・開始できるsudo設定が必要です。ステータス・送信設定ファイルはモード`0600`で書き込み、コマンドファイルも`0600`で配置します。

JSON Schemaとコマンド例は `config/device-command.schema.json` および `config/device-command.*.json` にあります。
