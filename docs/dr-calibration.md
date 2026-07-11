# DR driving calibration

`dr_calibrate.py` はQLM29Hの走行校正状態を監視し、校正完了後に校正データを保存するためのツールです。

## 実行条件

- QLM29Hを車体へ強固に固定し、走行中に位置や角度が変わらないようにします。
- 空が開け、GNSSを良好に受信できる平坦な場所で実施します。
- `qlm29h-nmea-unified.service` と同じシリアルポートを同時利用できないため、サービスを停止してから実行します。

## 実行手順

```bash
sudo systemctl stop qlm29h-nmea-unified.service

cd /home/pi/qlm29h-samples
./.venv/bin/python dr_calibrate.py \
  --serial-port /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 \
  --timeout 900

sudo systemctl start qlm29h-nmea-unified.service
```

プログラムを開始したら、7.2 km/h（2 m/s）を超える速度で走行し、3～4回右左折します。ガイド上の校正時間の目安は約3分です。

`CalState=2`へ到達すると校正完了です。既定ではDR hot start mode 2を有効化し、`PQTMDRSAVE`を送って校正データを保存して、終了コード0で終了します。mode 2では再起動時に校正データを利用しますが、GNSS信号がない起動直後には位置を出力しません。

## 状態の確認だけ行う

```bash
./.venv/bin/python dr_calibrate.py \
  --serial-port /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 \
  --status-only
```

| CalState | 意味 |
|---:|---|
| 0 | 未校正 |
| 1 | 軽度校正 |
| 2 | 校正完了 |
| 3 | 高精度方位まで校正完了 |

| NavType | 意味 |
|---:|---|
| 0 | 測位なし |
| 1 | GNSSのみ |
| 2 | DRのみ |
| 3 | GNSSとDRの複合測位 |

## オプション

- `--clear`: 既存の校正データを消して最初から校正します。通常の再校正では、取付位置や角度を変更した場合にだけ指定します。
- `--minimum-state 3`: 高精度方位校正まで待ちます。既定値は`2`です。
- `--hot-start-mode 1|2|unchanged`: DR hot startを設定します。既定値は、安全側の動作となる`2`です。
- `--no-save`: 完了後に`PQTMDRSAVE`を送りません。hot start mode 1/2には自動保存機能があるため、保存を完全に避ける検証では`--hot-start-mode unchanged`も指定します。
- `--keep-message-output`: 終了時に`PQTMDRCAL`の出力レートを元へ戻さず、1 Hz出力を維持します。

Ctrl+C、タイムアウト、通信エラーの場合も、`PQTMDRCAL`の出力レートは可能な限り実行前の値へ戻します。プログラム終了後は、成否にかかわらずsystemdサービスを再開してください。校正中は通常のUnified Endpoint送信デーモンを停止するため、その時間帯の送信データは生成されません。
