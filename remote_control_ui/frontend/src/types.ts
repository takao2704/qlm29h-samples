export type Preset = 'full' | 'compact' | 'position' | 'custom'
export type Sentence = 'GGA' | 'RMC' | 'GSA' | 'GSV' | 'GLL' | 'VTG'

export interface RuntimeConfig {
  apiBaseUrl: string
  deviceId: string
  cognitoIssuer: string
  cognitoDomain: string
  cognitoClientId: string
  mockMode: string
}

export interface PayloadControl {
  version: number
  enabled: boolean
  preset: Preset
  interval_sec: number
  include_sentences: Sentence[] | null
  [key: string]: unknown
}

export interface ControlStatus {
  request_id: string
  action: string
  status: string
  updated_at: string
  calibration_state?: number
  navigation_type?: number
  target_request_id?: string
  error?: string
}

export interface TelemetryStatus {
  version?: number
  updated_at?: string
  latest_data_received_at?: string | null
  latest_position?: {
    lat?: number
    lon?: number
    received_at?: string
    quality?: number
    quality_label?: string
    satellites_used?: number
    hdop?: number
    altitude?: number
  } | null
  rtk?: { quality?: number | null; quality_label?: string }
  dr?: { configured?: 'on' | 'off' | 'unchanged'; active?: boolean }
  ntrip?: {
    status?: string
    connected_at?: string
    last_received_at?: string | null
    last_bytes?: number
    total_bytes?: number
    error?: string
  }
  satellites?: { used?: number | null; in_view?: number | null; constellations?: string[] }
}

export interface DeviceStatus {
  version: number
  device_id: string
  online: boolean
  observed_at: string
  transmission_enabled: boolean
  payload_control: PayloadControl
  control_status: ControlStatus | null
  telemetry_status?: TelemetryStatus | null
}

export interface HistoryItem {
  request_id: string
  action: string
  created_at: string
  result: { status?: string }
  parameters?: Record<string, unknown>
}
