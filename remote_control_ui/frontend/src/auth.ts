import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'
import type { RuntimeConfig } from './types'

export class DashboardAuth {
  private manager: UserManager | null = null

  constructor(private readonly config: RuntimeConfig) {
    if (config.mockMode === 'true') return
    const origin = `${window.location.origin}/`
    this.manager = new UserManager({
      authority: config.cognitoIssuer,
      client_id: config.cognitoClientId,
      redirect_uri: origin,
      post_logout_redirect_uri: origin,
      response_type: 'code',
      scope: 'openid email',
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      metadata: {
        issuer: config.cognitoIssuer,
        authorization_endpoint: `${config.cognitoDomain}/oauth2/authorize`,
        token_endpoint: `${config.cognitoDomain}/oauth2/token`,
        userinfo_endpoint: `${config.cognitoDomain}/oauth2/userInfo`,
        end_session_endpoint: `${config.cognitoDomain}/logout`,
        jwks_uri: `${config.cognitoIssuer}/.well-known/jwks.json`,
      },
    })
  }

  async initialize(): Promise<User | null> {
    if (!this.manager) return null
    if (window.location.search.includes('code=')) {
      const user = await this.manager.signinRedirectCallback()
      window.history.replaceState({}, document.title, window.location.pathname)
      return user
    }
    return this.manager.getUser()
  }

  login() {
    return this.manager?.signinRedirect()
  }

  async logout() {
    if (!this.manager) return
    await this.manager.removeUser()
    const returnTo = `${window.location.origin}/`
    window.location.assign(
      `${this.config.cognitoDomain}/logout?client_id=${encodeURIComponent(this.config.cognitoClientId)}&logout_uri=${encodeURIComponent(returnTo)}`,
    )
  }

  async accessToken(): Promise<string> {
    if (!this.manager) return 'mock-access-token'
    const user = await this.manager.getUser()
    if (!user || user.expired) throw new Error('ログインの有効期限が切れました')
    return user.access_token
  }
}
