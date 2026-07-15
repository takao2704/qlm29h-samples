import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronRight,
  Cloud,
  Gauge,
  LogIn,
  LogOut,
  Navigation,
  Radio,
  RefreshCw,
  RotateCcw,
  Satellite,
  Send,
  Settings2,
  ShieldCheck,
  Signal,
  Square,
  X,
} from 'lucide-react'
import type { User } from 'oidc-client-ts'
import { RemoteApi, loadRuntimeConfig } from './api'
import { DashboardAuth } from './auth'
import type {
  DeviceStatus,
  HistoryItem,
  PayloadControl,
  Preset,
  RuntimeConfig,
  Sentence,
} from './types'

const sentences: { id: Sentence; label: string; description: string }[] = [
  { id: 'GGA', label: 'GGA', description: '位置・高度・Fix品質' },
  { id: 'RMC', label: 'RMC', description: '位置・速度・日時' },
  { id: 'GSA', label: 'GSA', description: '測位モード・DOP' },
  { id: 'GSV', label: 'GSV', description: '衛星情報' },
  { id: 'GLL', label: 'GLL', description: '緯度・経度' },
  { id: 'VTG', label: 'VTG', description: '進行方向・速度' },
]

const presetCopy: Record<Preset, { title: string; description: string }> = {
  full: { title: 'Full', description: 'すべてのNMEAと生データ' },
  compact: { title: 'Compact', description: '解析値中心の軽量データ' },
  position: { title: 'Position', description: '最新位置の要約のみ' },
  custom: { title: 'Custom', description: '文種を個別に選択' },
}

const actionLabels: Record<string, string> = {
  transmission_start: '送信を開始',
  transmission_stop: '送信を停止',
  payload_config_update: '送信設定を更新',
  dr_alignment_start: 'DRアライメント開始',
  dr_alignment_cancel: 'DRアライメント中止',
}

const defaultPayload: PayloadControl = {
  version: 1,
  enabled: true,
  preset: 'compact',
  interval_sec: 5,
  include_sentences: ['GGA', 'RMC', 'GSA'],
}

export default function App() {
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null)
  const [auth, setAuth] = useState<DashboardAuth | null>(null)
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [device, setDevice] = useState<DeviceStatus | null>(null)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [draft, setDraft] = useState<PayloadControl>(defaultPayload)
  const [busy, setBusy] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [drDialog, setDrDialog] = useState(false)
  const [clearExisting, setClearExisting] = useState(false)
  const initializedDraft = useRef(false)

  useEffect(() => {
    loadRuntimeConfig()
      .then(async (value) => {
        setRuntime(value)
        const session = new DashboardAuth(value)
        setAuth(session)
        setUser(await session.initialize())
      })
      .catch((reason) => setError(messageOf(reason)))
  }, [])

  const api = useMemo(() => {
    if (!runtime || !auth) return null
    return new RemoteApi(runtime, () => auth.accessToken())
  }, [runtime, auth])

  const refresh = useCallback(
    async (showSpinner = false) => {
      if (!api || (runtime?.mockMode !== 'true' && !user)) return
      if (showSpinner) setRefreshing(true)
      try {
        const [nextDevice, nextHistory] = await Promise.all([api.getDevice(), api.getHistory()])
        setDevice(nextDevice)
        setHistory(nextHistory)
        setError(null)
        if (!initializedDraft.current) {
          setDraft(normalizeDraft(nextDevice.payload_control))
          initializedDraft.current = true
        }
      } catch (reason) {
        setError(messageOf(reason))
      } finally {
        setRefreshing(false)
      }
    },
    [api, runtime?.mockMode, user],
  )

  useEffect(() => {
    void refresh(true)
    const timer = window.setInterval(() => void refresh(false), 15000)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4500)
    return () => window.clearTimeout(timer)
  }, [notice])

  const execute = async (action: string, parameters: Record<string, unknown> = {}, success: string) => {
    if (!api) return
    setBusy(action)
    setError(null)
    try {
      await api.sendCommand(action, parameters)
      setNotice(success)
      await refresh(false)
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(null)
    }
  }

  const toggleTransmission = () => {
    const enabled = device?.transmission_enabled ?? draft.enabled
    const action = enabled ? 'transmission_stop' : 'transmission_start'
    void execute(action, {}, enabled ? 'データ送信を停止しました' : 'データ送信を開始しました')
  }

  const applyPayload = () => {
    const configuration: PayloadControl = {
      ...draft,
      version: 1,
      enabled: device?.transmission_enabled ?? draft.enabled,
      include_sentences: draft.preset === 'custom' ? draft.include_sentences : null,
    }
    void execute(
      'payload_config_update',
      { configuration },
      '送信するデータを更新しました',
    )
  }

  const startAlignment = () => {
    setDrDialog(false)
    void execute(
      'dr_alignment_start',
      {
        timeout_sec: 900,
        minimum_state: 2,
        clear_existing: clearExisting,
        hot_start_mode: '2',
        save_on_complete: true,
        keep_message_output: false,
      },
      'DRアライメントを開始しました。安全な場所で走行してください',
    )
  }

  const cancelAlignment = () => {
    const requestId = device?.control_status?.request_id
    if (!requestId) return
    void execute(
      'dr_alignment_cancel',
      { target_request_id: requestId },
      'DRアライメントの中止を受け付けました',
    )
  }

  if (!runtime || user === undefined) return <LoadingScreen />
  if (runtime.mockMode !== 'true' && !user) {
    return <LoginScreen onLogin={() => void auth?.login()} error={error} />
  }

  const selected = draft.include_sentences ?? sentences.map((item) => item.id)
  const isAlignmentRunning =
    device?.control_status?.action === 'dr_alignment_start' &&
    ['accepted', 'stopping_sender', 'running'].includes(device.control_status.status)
  const estimatedBytes = estimatePayloadBytes(draft)

  return (
    <div className="app-shell">
      <aside className="status-rail">
        <div className="brand-block">
          <div className="brand-mark"><Satellite size={21} strokeWidth={2.2} /></div>
          <div>
            <div className="brand-title">QLM29H</div>
            <div className="brand-subtitle">Remote Control</div>
          </div>
        </div>

        <section className="rail-section device-identity">
          <div className="section-eyebrow">DEVICE</div>
          <div className="device-name">{device?.device_id ?? runtime.deviceId}</div>
          <div className={`health-pill ${device?.online ? 'healthy' : 'offline'}`}>
            <span className="health-dot" />
            {device?.online ? 'オンライン' : '応答なし'}
          </div>
        </section>

        <section className="rail-section connection-list">
          <ConnectionRow icon={<Signal size={17} />} label="セルラー" value={device?.online ? '接続中' : '未確認'} ok={!!device?.online} />
          <ConnectionRow icon={<Cloud size={17} />} label="Remote Command" value={device?.online ? '到達可能' : '未確認'} ok={!!device?.online} />
          <ConnectionRow icon={<ShieldCheck size={17} />} label="API保護" value="Cognito" ok />
        </section>

        <section className="rail-section current-config">
          <div className="section-eyebrow">CURRENT PAYLOAD</div>
          <div className="summary-row"><span>プリセット</span><strong>{presetCopy[device?.payload_control.preset ?? draft.preset].title}</strong></div>
          <div className="summary-row"><span>間隔</span><strong>{device?.payload_control.interval_sec ?? draft.interval_sec} 秒</strong></div>
          <div className="summary-row"><span>文種</span><strong>{sentenceSummary(device?.payload_control)}</strong></div>
        </section>

        <section className="rail-section recent-command">
          <div className="section-eyebrow">RECENT COMMAND</div>
          {device?.control_status ? (
            <>
              <div className="recent-action">{actionLabels[device.control_status.action] ?? device.control_status.action}</div>
              <div className="recent-meta">
                <StatusBadge status={device.control_status.status} />
                <span>{formatRelative(device.control_status.updated_at)}</span>
              </div>
            </>
          ) : <div className="muted">実行履歴はありません</div>}
        </section>

        <button className="rail-logout" onClick={() => void auth?.logout()}>
          <LogOut size={16} /> ログアウト
        </button>
      </aside>

      <main className="main-content">
        <header className="page-header">
          <div>
            <div className="breadcrumb">QLM29H / {device?.device_id ?? runtime.deviceId}</div>
            <h1>Remote Control</h1>
            <p>送信データとDRアライメントを、安全なリモートコマンドで操作します。</p>
          </div>
          <button className="icon-button" aria-label="状態を更新" onClick={() => void refresh(true)} disabled={refreshing}>
            <RefreshCw size={18} className={refreshing ? 'spin' : ''} />
          </button>
        </header>

        {error && <div className="alert error-alert"><AlertTriangle size={18} /><span>{error}</span><button onClick={() => setError(null)}><X size={16} /></button></div>}
        {notice && <div className="alert success-alert"><Check size={18} /><span>{notice}</span></div>}

        <section className="panel transmission-panel">
          <div className="panel-heading">
            <div className="panel-icon blue"><Send size={20} /></div>
            <div className="panel-title-group">
              <h2>データ送信</h2>
              <p>Unified Endpointへ送るデータを選択します。</p>
            </div>
            <div className="transmission-control">
              <div>
                <div className="control-label">送信状態</div>
                <strong>{device?.transmission_enabled ? '送信中' : '停止中'}</strong>
              </div>
              <button
                className={`toggle ${device?.transmission_enabled ? 'on' : ''}`}
                role="switch"
                aria-checked={!!device?.transmission_enabled}
                aria-label="データ送信の開始・停止"
                onClick={toggleTransmission}
                disabled={busy !== null}
              ><span /></button>
            </div>
          </div>

          <div className="panel-body">
            <div className="field-label">送信プリセット</div>
            <div className="preset-grid">
              {(Object.keys(presetCopy) as Preset[]).map((preset) => (
                <button
                  key={preset}
                  className={`preset-card ${draft.preset === preset ? 'selected' : ''}`}
                  onClick={() => setDraft((current) => ({
                    ...current,
                    preset,
                    include_sentences: preset === 'custom' ? current.include_sentences : null,
                  }))}
                >
                  <span className="preset-check">{draft.preset === preset && <Check size={14} />}</span>
                  <strong>{presetCopy[preset].title}</strong>
                  <small>{presetCopy[preset].description}</small>
                </button>
              ))}
            </div>

            <div className="config-row">
              <label className="interval-field">
                <span className="field-label">送信間隔</span>
                <span className="number-input-wrap">
                  <input
                    type="number"
                    min="1"
                    max="3600"
                    value={draft.interval_sec}
                    onChange={(event) => setDraft((current) => ({ ...current, interval_sec: Number(event.target.value) }))}
                  />
                  <span>秒</span>
                </span>
              </label>
              <div className="estimate-card">
                <Gauge size={18} />
                <span>1回あたりの概算</span>
                <strong>{estimatedBytes.toLocaleString()} bytes</strong>
              </div>
            </div>

            <div className="sentence-section">
              <div className="field-label">送信するNMEA文種</div>
              {draft.preset === 'position' && <div className="inline-note">Positionプリセットでは、NMEA文種ではなく最新位置の要約を送信します。</div>}
              <div className={`sentence-grid ${draft.preset === 'position' ? 'disabled' : ''}`}>
                {sentences.map((sentence) => {
                  const checked = selected.includes(sentence.id)
                  return (
                    <label className="sentence-option" key={sentence.id}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={draft.preset === 'position'}
                        onChange={() => {
                          const next = checked
                            ? selected.filter((item) => item !== sentence.id)
                            : [...selected, sentence.id]
                          setDraft((current) => ({ ...current, preset: 'custom', include_sentences: next }))
                        }}
                      />
                      <span className="custom-checkbox">{checked && <Check size={13} />}</span>
                      <span><strong>{sentence.label}</strong><small>{sentence.description}</small></span>
                    </label>
                  )
                })}
              </div>
            </div>

            <div className="panel-actions">
              <div className="safe-note"><ShieldCheck size={16} /> 設定はRemote Command経由でデバイスへ直接反映されます</div>
              <button className="primary-button" onClick={applyPayload} disabled={busy !== null || draft.interval_sec < 1}>
                {busy === 'payload_config_update' ? <RefreshCw size={17} className="spin" /> : <Settings2 size={17} />}
                送信設定を適用
              </button>
            </div>
          </div>
        </section>

        <section className="panel dr-panel">
          <div className="panel-heading">
            <div className="panel-icon amber"><Navigation size={20} /></div>
            <div className="panel-title-group">
              <h2>DRアライメント</h2>
              <p>走行校正の状態確認と開始・中止を行います。</p>
            </div>
            <StatusBadge status={isAlignmentRunning ? 'running' : calibrationLabel(device)} />
          </div>
          <div className="dr-body">
            <div className="calibration-state">
              <div className="calibration-gauge">
                <div className="gauge-value">{device?.control_status?.calibration_state ?? 0}</div>
                <div className="gauge-total">/ 3</div>
              </div>
              <div>
                <div className="field-label">CALIBRATION STATE</div>
                <h3>{calibrationText(device?.control_status?.calibration_state)}</h3>
                <p>{navigationText(device?.control_status?.navigation_type)}</p>
              </div>
            </div>
            <div className="safety-card">
              <AlertTriangle size={20} />
              <div>
                <strong>安全な走行環境を確認してください</strong>
                <p>屋外の開けた場所で2 m/s以上、3〜4回曲がる走行が必要です。開始中は通常のNMEA送信を一時停止します。</p>
              </div>
            </div>
            <div className="dr-actions">
              {isAlignmentRunning ? (
                <button className="danger-button" onClick={cancelAlignment} disabled={busy !== null}>
                  <Square size={16} /> アライメントを中止
                </button>
              ) : (
                <button className="secondary-button" onClick={() => setDrDialog(true)} disabled={busy !== null}>
                  <Navigation size={17} /> DRアライメントを開始 <ChevronRight size={16} />
                </button>
              )}
            </div>
          </div>
        </section>

        <section className="panel history-panel">
          <div className="history-heading">
            <div>
              <h2>コマンド履歴</h2>
              <p>直近のリモート操作と受付結果です。</p>
            </div>
            <Activity size={20} />
          </div>
          <div className="history-table-wrap">
            <table>
              <thead><tr><th>操作</th><th>状態</th><th>実行日時</th><th>Request ID</th></tr></thead>
              <tbody>
                {history.length === 0 ? (
                  <tr><td colSpan={4} className="empty-row">操作履歴はまだありません</td></tr>
                ) : history.slice(0, 8).map((item) => (
                  <tr key={item.request_id}>
                    <td><span className="action-cell"><Radio size={15} />{actionLabels[item.action] ?? item.action}</span></td>
                    <td><StatusBadge status={item.result?.status ?? 'accepted'} /></td>
                    <td>{formatDate(item.created_at)}</td>
                    <td className="request-id">{item.request_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <footer className="page-footer">
          <span><span className="health-dot" /> Last response: {device ? formatRelative(device.observed_at) : '—'}</span>
          <span>SORACOM Remote Command HTTP</span>
        </footer>
      </main>

      {drDialog && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setDrDialog(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="dr-dialog-title">
            <button className="modal-close" aria-label="閉じる" onClick={() => setDrDialog(false)}><X size={18} /></button>
            <div className="modal-icon"><Navigation size={24} /></div>
            <h2 id="dr-dialog-title">DRアライメントを開始しますか？</h2>
            <p>開始するとデータ送信を一時停止し、最大15分間アライメント状態を監視します。終了後は送信を自動で再開します。</p>
            <div className="modal-safety"><AlertTriangle size={18} /><span>停車中に開始し、安全を確認してから走行してください。</span></div>
            <label className="clear-option">
              <input type="checkbox" checked={clearExisting} onChange={(event) => setClearExisting(event.target.checked)} />
              <span className="custom-checkbox">{clearExisting && <Check size={13} />}</span>
              <span><strong>既存の校正データを消去する</strong><small>受信機の取付位置や角度を変更した場合だけ選択します。</small></span>
            </label>
            {clearExisting && <div className="destructive-warning"><RotateCcw size={17} /> 保存済みのDR校正データが消去されます。</div>}
            <div className="modal-actions">
              <button className="ghost-button" onClick={() => setDrDialog(false)}>キャンセル</button>
              <button className="primary-button" onClick={startAlignment}><Navigation size={17} />開始する</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ConnectionRow({ icon, label, value, ok }: { icon: React.ReactNode; label: string; value: string; ok: boolean }) {
  return <div className="connection-row"><span className="connection-icon">{icon}</span><span>{label}</span><strong className={ok ? 'ok' : ''}>{value}</strong></div>
}

function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase()
  const good = ['completed', 'accepted', 'cancelled', 'calibrated'].includes(normalized)
  const active = ['running', 'stopping_sender', 'cancelling'].includes(normalized)
  const label: Record<string, string> = {
    completed: '完了', accepted: '受付済み', cancelled: '中止済み', running: '実行中',
    stopping_sender: '準備中', cancelling: '中止中', failed: '失敗', invalid: '不正',
    calibrated: '校正済み', uncalibrated: '未校正',
  }
  return <span className={`status-badge ${good ? 'good' : active ? 'active' : 'warn'}`}><span />{label[normalized] ?? status}</span>
}

function LoadingScreen() {
  return <div className="center-screen"><div className="loading-logo"><Satellite size={28} /></div><RefreshCw className="spin" size={20} /><span>操作画面を準備しています</span></div>
}

function LoginScreen({ onLogin, error }: { onLogin: () => void; error: string | null }) {
  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="loading-logo"><Satellite size={28} /></div>
        <div className="section-eyebrow">QLM29H DEVICE CONSOLE</div>
        <h1>Remote Control</h1>
        <p>送信データとDRアライメントを遠隔操作するには、管理者としてログインしてください。</p>
        {error && <div className="login-error">{error}</div>}
        <button className="primary-button login-button" onClick={onLogin}><LogIn size={18} />ログイン</button>
        <div className="login-security"><ShieldCheck size={15} /> Cognito + Authorization Code / PKCE</div>
      </div>
    </div>
  )
}

function normalizeDraft(payload: PayloadControl): PayloadControl {
  return {
    version: 1,
    enabled: payload.enabled,
    preset: payload.preset,
    interval_sec: payload.interval_sec ?? 5,
    include_sentences: payload.include_sentences ?? sentences.map((item) => item.id),
  }
}

function sentenceSummary(payload?: PayloadControl): string {
  if (!payload) return '—'
  if (payload.preset === 'position') return 'Position'
  const list = payload.include_sentences
  return list === null || list === undefined ? 'すべて' : `${list.length} 種類`
}

function estimatePayloadBytes(payload: PayloadControl): number {
  if (payload.preset === 'position') return 280
  const count = payload.include_sentences?.length ?? sentences.length
  const perSentence = payload.preset === 'full' ? 520 : payload.preset === 'compact' ? 260 : 330
  return 180 + count * perSentence
}

function calibrationText(state?: number) {
  if (state === 3) return '高精度校正済み'
  if (state === 2) return '校正完了'
  if (state === 1) return '校正進行中'
  return '未校正'
}

function navigationText(type?: number) {
  if (type === 3) return 'GNSS + DR 統合測位'
  if (type === 2) return 'DR測位'
  return 'GNSS測位のみ'
}

function calibrationLabel(device: DeviceStatus | null): string {
  return (device?.control_status?.calibration_state ?? 0) >= 2 ? 'calibrated' : 'uncalibrated'
}

function formatRelative(value?: string) {
  if (!value) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000))
  if (seconds < 5) return 'たった今'
  if (seconds < 60) return `${seconds}秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分前`
  return formatDate(value)
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value))
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作に失敗しました'
}
