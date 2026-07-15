import type { DeviceStatus, HistoryItem, PayloadControl, RuntimeConfig } from './types'

const now = () => new Date().toISOString()

const defaultPayload: PayloadControl = {
  version: 1,
  enabled: true,
  preset: 'compact',
  interval_sec: 5,
  include_sentences: ['GGA', 'RMC', 'GSA'],
}

let mockStatus: DeviceStatus = {
  version: 1,
  device_id: 'takao_01s_05',
  online: true,
  observed_at: now(),
  transmission_enabled: true,
  payload_control: defaultPayload,
  control_status: {
    request_id: 'web-demo-1',
    action: 'transmission_start',
    status: 'completed',
    updated_at: now(),
    calibration_state: 0,
    navigation_type: 1,
  },
}

const mockHistory: HistoryItem[] = [
  {
    request_id: 'web-demo-1',
    action: 'transmission_start',
    created_at: now(),
    result: { status: 'accepted' },
  },
]

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch('/runtime-config.json', { cache: 'no-store' })
  if (!response.ok) throw new Error('画面設定を読み込めませんでした')
  return response.json()
}

export class RemoteApi {
  constructor(
    private readonly config: RuntimeConfig,
    private readonly accessToken: () => Promise<string>,
  ) {}

  private get mockMode() {
    return this.config.mockMode === 'true'
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = await this.accessToken()
    const response = await fetch(`${this.config.apiBaseUrl}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        ...init.headers,
      },
    })
    const value = await response.json()
    if (!response.ok) throw new Error(value.error || '操作に失敗しました')
    return value
  }

  async getDevice(): Promise<DeviceStatus> {
    if (this.mockMode) {
      await wait(180)
      mockStatus = { ...mockStatus, observed_at: now() }
      return structuredClone(mockStatus)
    }
    return this.request('/api/device')
  }

  async getHistory(): Promise<HistoryItem[]> {
    if (this.mockMode) return structuredClone(mockHistory)
    const response = await this.request<{ items: HistoryItem[] }>('/api/history')
    return response.items
  }

  async sendCommand(action: string, parameters: Record<string, unknown> = {}) {
    if (this.mockMode) {
      await wait(420)
      const requestId = `web-demo-${Date.now()}`
      if (action === 'transmission_start' || action === 'transmission_stop') {
        const enabled = action === 'transmission_start'
        mockStatus.transmission_enabled = enabled
        mockStatus.payload_control.enabled = enabled
      }
      if (action === 'payload_config_update') {
        const configuration = parameters.configuration as PayloadControl
        mockStatus.payload_control = structuredClone(configuration)
        mockStatus.transmission_enabled = configuration.enabled
      }
      if (action === 'dr_alignment_start') {
        mockStatus.control_status = {
          request_id: requestId,
          action,
          status: 'running',
          updated_at: now(),
          calibration_state: 0,
          navigation_type: 1,
        }
      } else if (action === 'dr_alignment_cancel') {
        mockStatus.control_status = {
          request_id: requestId,
          action,
          status: 'cancelled',
          updated_at: now(),
        }
      } else {
        mockStatus.control_status = {
          request_id: requestId,
          action,
          status: 'completed',
          updated_at: now(),
        }
      }
      mockHistory.unshift({
        request_id: requestId,
        action,
        created_at: now(),
        result: { status: 'accepted' },
        parameters,
      })
      return { request_id: requestId, action, status: 'accepted' }
    }
    return this.request('/api/commands', {
      method: 'POST',
      body: JSON.stringify({ action, parameters }),
    })
  }
}

function wait(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}
