# 送信データ制御インターフェース

`rtk_nmea_unified.py` は、次のJSON設定ファイルを自動的に読み込みます。

```text
/home/pi/.config/qlm29h/payload-control.json
```

ファイルが存在しない場合は、従来どおり5秒間隔の完全なペイロードを送信します。ファイルを変更すると、デーモンを再起動せずに新しい設定が反映されます。JSONが壊れている場合や未知の項目がある場合は、直前の正常な設定を維持します。

## プリセット

| preset | 用途 | 内容 |
|---|---|---|
| `full` | 調査・デバッグ | 現在と同じ完全なNMEAデータ |
| `compact` | 通常運用 | 全NMEAの解析値を残し、原文・raw値・重複メタデータを除外 |
| `position` | 位置監視 | `latest_position` とトップレベルの位置情報だけを送信 |
| `custom` | 個別制御 | NMEA文種、フィールド、raw情報などを個別指定 |

使用例は次のファイルにあります。

- `config/payload-control.full.json`
- `config/payload-control.compact.json`
- `config/payload-control.position.json`
- `config/payload-control.custom.json`

設定モデルをUIや別プログラムから利用するためのJSON Schemaは `config/payload-control.schema.json` です。

## 設定項目

| 項目 | 型 | 説明 |
|---|---|---|
| `version` | number | 現在は `1` |
| `enabled` | boolean | `false` の間は新しいペイロードを送信しない |
| `interval_sec` | number | 送信間隔。0より大きい秒数 |
| `preset` | string | `full`、`compact`、`position`、`custom` |
| `include_nmea` | boolean | `nmea` オブジェクトを含める |
| `include_sentences` | array/null | 送る文種。`null` はすべて。`GNGGA`または`GGA`のように指定可能 |
| `exclude_sentences` | array | 除外する文種 |
| `include_raw_sentence` | boolean | NMEA原文の `raw` を含める |
| `include_raw_fields` | boolean | `raw_fields`、`raw_field_XX`、`*_raw` を含める |
| `include_parsed_fields` | boolean | 型変換済みの `fields` を含める |
| `include_nmea_metadata` | boolean | checksum、talker、受信時刻などを含める |
| `include_satellite_details` | boolean | GSVの衛星ごとの詳細配列を含める |
| `include_sentence_counts` | boolean | 収集期間内のNMEA文数を含める |
| `include_latest_position` | boolean | `latest_position` を含める |
| `include_position_aliases` | boolean | トップレベルの `lat`、`lon`、`quality` を含める |
| `field_allowlist` | object | 文種ごとに送る `fields` のキーを限定する |

`include_sentences`、`exclude_sentences`、`field_allowlist` では、`GNGGA`のような完全なSentence IDと、`GGA`のようなMessage Typeの両方を使用できます。

## 停止と再開

送信を止める場合は、次の設定にします。NTRIP補正とGNSS受信は継続し、Unified Endpointへの新しい送信だけを止めます。

```json
{
  "version": 1,
  "enabled": false,
  "preset": "position"
}
```

`enabled` を `false` にすると、新規ペイロード生成とスプール済みデータの送信を両方停止します。進行中のHTTPリクエストだけは中断せず、設定されたタイムアウトまで完了を待ちます。

`enabled` を `true` に戻すと、保留されていたスプール済みデータを送信してから通常送信を再開します。設定変更時に収集中のスナップショットはリセットされるため、停止中に受信したデータを再開後にまとめて送ることはありません。

## 別の設定ファイルを使う場合

コマンドラインの `--payload-control`、または環境変数 `UNIFIED_PAYLOAD_CONTROL` でパスを変更できます。

```text
UNIFIED_PAYLOAD_CONTROL=/path/to/payload-control.json
```

この設定ファイルはデバイス内の制御インターフェースです。将来Web UIやSORACOM経由の遠隔制御を追加する場合も、このJSONモデルを更新する構成にすることで送信処理を共通化できます。

送信開始・停止を一回限りのコマンドとして実行する場合や、DRアライメントを開始する場合は `docs/device-control.md` のデバイス制御インターフェースを使用します。
