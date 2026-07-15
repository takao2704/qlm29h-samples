# QLM29H Samples

Quectel QLM29HBAA-GM 用の RTK（NTRIP）サンプルスクリプト集です。

## ファイル構成

| ファイル | 説明 |
|---|---|
| [rtk_client.py](rtk_client.py) | NTRIPクライアント。RTCM補正をモジュールに転送し、Fix状態をログ表示する基本サンプル |
| [rtk_harvest.py](rtk_harvest.py) | `rtk_client.py` に加え、GNSS位置情報を SORACOM Harvest Data（Unified Endpoint）へ定期送信するサンプル |
| [rtk_nmea_unified.py](rtk_nmea_unified.py) | NTRIP補正、NMEA全文の構造化、Unified Endpointへのスプール付き送信を行うデーモン向けサンプル |
| [dr_calibrate.py](dr_calibrate.py) | QLM29Hの走行校正状態を監視し、完了後に校正データを保存するツール |
| [device_control.py](device_control.py) | JSONコマンドから送信開始・停止とDR走行校正の開始・中止を制御する常駐サービス |
| [remote_command_http.py](remote_command_http.py) | SORACOM Remote Command HTTPを既存のJSON制御へ接続する受付サービス |
| [remote_control_ui/](remote_control_ui/) | Lambda、API Gateway、Cognitoを使うブラウザ操作画面 |

## ドキュメント

| ファイル | 説明 |
|---|---|
| [docs/sequence.md](docs/sequence.md) | 4つのPythonスクリプトの実行シーケンス |
| [docs/dr-calibration.md](docs/dr-calibration.md) | DR走行校正ツールの実行条件、手順、判定方法 |
| [docs/payload-control.md](docs/payload-control.md) | 送信間隔、NMEA文種、フィールドを制御するJSON設定 |
| [docs/device-control.md](docs/device-control.md) | 送信開始・停止、DR走行校正を実行するJSONコマンドとステータス |

## セットアップ

```bash
pip3 install -r requirements.txt
```

NTRIP の認証情報は環境変数で渡してください（SORACOMから提供されます）。

```bash
export NTRIP_USER="..."
export NTRIP_PASS="..."
export QLM29H_DR_STATE=on
```

3つのスクリプトは起動時にRTKを有効化し、既定ではDRも有効化します。
`QLM29H_DR_STATE` は `on`、`off`、`unchanged` から選択できます。`unchanged` はDRコマンドを送らず、受信機の現在設定を維持します。
DRを有効化しても校正が完了するわけではありません。車体へ固定し、`PQTMDRCAL` の `CalState=2` または `3` を確認してからDR測位結果を評価してください。

## 実行方法

```bash
python3 rtk_client.py
# または
python3 rtk_harvest.py
```

シリアルポートやNTRIP接続先などは各スクリプト先頭の設定セクションを編集してください。

### DR走行校正

`dr_calibrate.py` は `PQTMDRCAL` を監視し、`CalState=2` または `3` への到達を判定します。
既存の送信デーモンとシリアルポートが競合するため、実行中はサービスを停止してください。

```bash
sudo systemctl stop qlm29h-nmea-unified.service
trap 'sudo systemctl start qlm29h-nmea-unified.service' EXIT
./.venv/bin/python dr_calibrate.py \
  --serial-port /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

詳細は [docs/dr-calibration.md](docs/dr-calibration.md) を参照してください。

### JSONによるデバイス制御

`qlm29h-device-control.service` を有効化すると、次のファイルへ一意な `request_id` を持つJSONコマンドを配置して操作できます。

```text
/home/pi/.config/qlm29h/device-command.json
```

対応アクションは `transmission_start`、`transmission_stop`、`payload_config_update`、`dr_alignment_start`、`dr_alignment_cancel` です。処理状態と結果は `/home/pi/.config/qlm29h/device-status.json` に出力されます。DR走行校正中はシリアルポート競合を避けるため送信デーモンを一時停止し、完了・失敗・中止後に自動再開します。

SORACOM Remote Command HTTPとLambda操作画面の構成は [remote_control_ui/README.md](remote_control_ui/README.md) を参照してください。

詳細は [docs/device-control.md](docs/device-control.md) を参照してください。

### `rtk_nmea_unified.py` のspool先

`rtk_nmea_unified.py` はUnified Endpoint送信用payloadを一度spoolしてから送信します。
SDカードへの継続書き込みを避けたい場合は、RAM上のtmpfsをspool先にできます。

```bash
export UNIFIED_SPOOL_STORAGE=ram
export UNIFIED_MAX_SPOOL_FILES=7200
export UNIFIED_MAX_SPOOL_BYTES=16777216
python3 rtk_nmea_unified.py
```

`UNIFIED_SPOOL_STORAGE=ram` では、`UNIFIED_SPOOL_DIR` 未指定時に `/dev/shm/qlm29h-nmea-unified/spool` を使います。
systemdで運用する場合は、`RuntimeDirectory` を使って `/run/qlm29h-nmea-unified/spool` を指定する構成も使えます。

```ini
RuntimeDirectory=qlm29h-nmea-unified
Environment=UNIFIED_SPOOL_STORAGE=ram
Environment=UNIFIED_SPOOL_DIR=/run/qlm29h-nmea-unified/spool
Environment=UNIFIED_MAX_SPOOL_FILES=7200
Environment=UNIFIED_MAX_SPOOL_BYTES=16777216
Environment=QLM29H_DR_STATE=on
```

`UNIFIED_MAX_SPOOL_FILES` はリングバッファの最大件数です。上限に達すると古いpayloadから削除します。
`UNIFIED_MAX_SPOOL_BYTES` はspool内JSON payloadの合計byte数上限です。`0` を指定するとbyte上限を無効化します。
RAM上のspoolは再起動で消えるため、SDカード保護を優先する用途向けです。

## テスト

```bash
python3 -m unittest discover -s tests
```

## ライセンス

このリポジトリのコードは [MIT License](LICENSE) で提供します。
本スクリプトが利用する `pyserial` / `requests` などのライブラリは、それぞれのライセンスに従います。

## 免責事項

- 本リポジトリ配下のスクリプトは開発例（サンプル）であり、動作を保証するものではありません。
- 本スクリプトに記載されている内容は商用利用を想定していません。ご利用にあたっては、お客様の責任の範囲においてご利用ください。
