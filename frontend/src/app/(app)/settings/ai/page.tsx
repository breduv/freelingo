'use client'

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import { apiFetch } from '@/lib/api'
import { useAuthStore } from '@/store/auth'
import {
  SettingsPageHeader,
  SettingsPanel,
} from '@/components/settings/SettingsShell'

type Provider = 'ollama' | 'openai' | 'anthropic' | 'deepseek' | 'chatgpt'

interface Options {
  provider: Provider
  api_base: string
  model: string
  timeout_seconds: number
  max_output_tokens: number | null
  temperature: number | null
  reasoning_effort: string
  native_tools: boolean
}

interface Configuration {
  options: Options
  has_api_key: boolean
  source: string
  chatgpt: { connected: boolean; plan_enabled: boolean; email: string }
  provider_defaults: Record<Provider, { options: Options; has_api_key: boolean }>
}

const inputClass =
  'border-fl-border bg-fl-bg text-fl-fg w-full border px-3 py-2 text-sm'
const buttonClass =
  'border-fl-border text-fl-fg hover:bg-fl-bg border px-4 py-2 text-sm disabled:opacity-40'

export default function AiSettingsPage() {
  const user = useAuthStore((s) => s.user)
  const t = useTranslations('aiSettings')
  if (user?.role !== 'admin') {
    return <p className="text-fl-muted-2 p-6">{t('adminOnly')}</p>
  }
  return <AiSettings key={user.id} />
}

function AiSettings() {
  const t = useTranslations('aiSettings')
  const [config, setConfig] = useState<Configuration | null>(null)
  const [options, setOptions] = useState<Options | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const running = useRef(false)

  async function request(path: string, method = 'GET', body?: object) {
    const signal = controller.current?.signal
    const response = await apiFetch(`/api/admin/llm${path}`, {
      method,
      signal,
      headers: { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    const data = await response.json()
    signal?.throwIfAborted()
    if (!response.ok) {
      throw new Error(
        typeof data.detail === 'string' ? data.detail : t('requestFailed')
      )
    }
    return data
  }

  async function load() {
    const data: Configuration = await request('')
    const connected =
      new URLSearchParams(window.location.search).get('chatgpt') === 'connected'
    setConfig(data)
    setOptions(
      connected
        ? { ...data.provider_defaults.chatgpt.options, model: '' }
        : data.options
    )
    setApiKey('')
    setClearKey(false)
    setMessage(
      connected
        ? t('connectedNext')
        : t(data.source === 'saved' ? 'savedSource' : 'envSource')
    )
    window.history.replaceState(null, '', '/settings/ai')
  }

  async function run(action: () => Promise<void>) {
    if (running.current) return
    running.current = true
    const signal = controller.current?.signal
    setBusy(true)
    setFailed(false)
    try {
      await action()
    } catch (error) {
      if (
        !signal?.aborted &&
        !(error instanceof DOMException && error.name === 'AbortError')
      ) {
        setFailed(true)
        setMessage(error instanceof Error ? error.message : t('requestFailed'))
      }
    } finally {
      if (!signal?.aborted) {
        running.current = false
        setBusy(false)
      }
    }
  }

  useEffect(() => {
    const current = new AbortController()
    controller.current = current
    running.current = false
    void run(load)
    return () => current.abort()
    // The keyed component owns one administrator session and cancels it on unmount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function change<K extends keyof Options>(key: K, value: Options[K]) {
    setOptions((previous) =>
      previous ? { ...previous, [key]: value } : previous
    )
  }

  function selectProvider(provider: Provider) {
    const defaults = config?.provider_defaults[provider]
    if (!defaults) return
    setOptions((previous) => ({
      ...defaults.options,
      timeout_seconds: previous?.timeout_seconds ?? defaults.options.timeout_seconds,
    }))
    setModels([])
    setApiKey('')
    setClearKey(false)
  }

  function payload() {
    return { ...options, api_key: apiKey, keep_api_key: !clearKey }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void run(async () => {
      await request('', 'PUT', payload())
      await load()
      setMessage(t('saved'))
    })
  }

  const chatgpt = options?.provider === 'chatgpt'
  const ollama = options?.provider === 'ollama'
  const reasoning = options?.provider === 'openai' || chatgpt
  const hasApiKey = options?.provider === config?.options.provider
    ? config?.has_api_key
    : options && config?.provider_defaults[options.provider].has_api_key

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6 font-sans">
      <Link href="/settings" className="text-fl-muted-2 text-sm hover:underline">
        ← {t('back')}
      </Link>
      <SettingsPageHeader
        title={t('title')}
        eyebrow={t('installation')}
        description={t('description')}
      />
      <p
        role="status"
        aria-live="polite"
        className={`border-fl-border bg-fl-surface border p-4 text-sm ${failed ? 'text-fl-error' : 'text-fl-muted-1'}`}
      >
        {busy ? t('working') : message}
      </p>
      {!options ? (
        <button
          className={buttonClass}
          disabled={busy}
          onClick={() => void run(load)}
        >
          {t('retry')}
        </button>
      ) : (
        <form onSubmit={save}>
          <fieldset disabled={busy} className="space-y-4 disabled:opacity-60">
            <SettingsPanel title={t('connection')}>
              <div className="border-fl-border bg-fl-surface space-y-4 border p-5">
                <label className="block space-y-2 text-sm">
                  <span>{t('source')}</span>
                  <select
                    className={inputClass}
                    value={options.provider}
                    onChange={(event) => selectProvider(event.target.value as Provider)}
                  >
                    <option value="ollama">Ollama</option>
                    <option value="openai">OpenAI</option>
                    <option value="anthropic">Anthropic / Claude</option>
                    <option value="deepseek">DeepSeek</option>
                    <option value="chatgpt">ChatGPT</option>
                  </select>
                </label>
                {chatgpt ? (
                  <div className="space-y-3 text-sm">
                    <p className="text-fl-muted-2">{t('planLimits')}</p>
                    <p>
                      {config?.chatgpt.connected
                        ? `${config.chatgpt.email} · ${t(config.chatgpt.plan_enabled ? 'planEnabled' : 'planMissing')}`
                        : t('notConnected')}
                    </p>
                    <div className="flex flex-wrap gap-3">
                      <button
                        type="button"
                        className={buttonClass}
                        data-oai-siwc-button
                        onClick={() => void run(async () => {
                          const result = await request('/chatgpt/connect', 'POST', {})
                          const url = new URL(result.url)
                          if (
                            url.protocol !== 'http:' ||
                            url.hostname !== '127.0.0.1' ||
                            url.pathname !== '/llm/auth/start'
                          ) {
                            throw new Error(t('requestFailed'))
                          }
                          window.location.assign(url.href)
                        })}
                      >
                        Continue with ChatGPT
                      </button>
                      {config?.chatgpt.connected && (
                        <button
                          type="button"
                          className={buttonClass}
                          onClick={() => void run(async () => {
                            const result = await request('/chatgpt/disconnect', 'POST', {})
                            await load()
                            setMessage(t(
                              result.remote_revocation_confirmed
                                ? 'disconnected'
                                : 'revokeManually'
                            ))
                          })}
                        >
                          {t('disconnect')}
                        </button>
                      )}
                    </div>
                    <a
                      href="https://chatgpt.com/settings/usage"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline"
                    >
                      {t('usage')}
                    </a>
                  </div>
                ) : (
                  <>
                    {ollama && (
                      <>
                        <label className="block space-y-2 text-sm">
                          <span className="font-code">OLLAMA_BASE_URL</span>
                          <input
                            className={inputClass}
                            type="url"
                            required
                            value={options.api_base}
                            onChange={(event) => change('api_base', event.target.value)}
                          />
                        </label>
                        <p className="text-fl-muted-2 text-sm">{t('endpointHelp')}</p>
                      </>
                    )}
                    <label className="block space-y-2 text-sm">
                      <span className="font-code">API_KEY</span>
                      <input
                        className={inputClass}
                        type="password"
                        autoComplete="new-password"
                        value={apiKey}
                        placeholder={t(hasApiKey ? 'keySaved' : 'keyOptional')}
                        onChange={(event) => setApiKey(event.target.value)}
                      />
                    </label>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={clearKey}
                        onChange={(event) => setClearKey(event.target.checked)}
                      />
                      {t('clearKey')}
                    </label>
                  </>
                )}
                <label className="block space-y-2 text-sm">
                  <span>{t('model')}</span>
                  <input
                    className={`${inputClass} font-code`}
                    required
                    list="ai-models"
                    value={options.model}
                    onChange={(event) => change('model', event.target.value)}
                  />
                </label>
                <datalist id="ai-models">
                  {models.map((model) => (
                    <option key={model.id} value={model.id} label={model.name} />
                  ))}
                </datalist>
                <button
                  type="button"
                  className={buttonClass}
                  onClick={() => void run(async () => {
                    const result = await request('/models', 'POST', {
                      ...payload(),
                      model: options.model || 'select-a-model',
                    })
                    setModels(result.models)
                    setMessage(t('modelsLoaded', { count: result.models.length }))
                  })}
                >
                  {t('loadModels')}
                </button>
              </div>
            </SettingsPanel>
            <details className="border-fl-border bg-fl-surface space-y-4 border p-5">
              <summary className="cursor-pointer text-sm">{t('advanced')}</summary>
              {([
                ['timeout_seconds', 5, 1800, 'timeout'],
                ['max_output_tokens', 64, 131072, 'outputTokens'],
                ['temperature', 0, 2, 'temperature'],
              ] as const).map(([key, min, max, label]) => (
                <label key={key} className="block space-y-2 text-sm">
                  <span>{t(label)}</span>
                  <input
                    className={inputClass}
                    type="number"
                    min={min}
                    max={key === 'temperature' && options.provider === 'anthropic' ? 1 : max}
                    step={key === 'temperature' ? 0.1 : 1}
                    required={key === 'timeout_seconds'}
                    disabled={chatgpt && key !== 'timeout_seconds'}
                    value={options[key] ?? ''}
                    onChange={(event) => change(
                      key,
                      event.target.value === ''
                        ? (key === 'timeout_seconds' ? 0 : null)
                        : Number(event.target.value)
                    )}
                  />
                </label>
              ))}
              {reasoning && <label className="block space-y-2 text-sm">
                <span>{t('reasoning')}</span>
                <select
                  className={inputClass}
                  value={options.reasoning_effort}
                  onChange={(event) => change('reasoning_effort', event.target.value)}
                >
                  {['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((effort) => (
                    <option key={effort} value={effort}>
                      {effort || t('serverDefault')}
                    </option>
                  ))}
                </select>
              </label>}
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={options.native_tools}
                  onChange={(event) => change('native_tools', event.target.checked)}
                />
                {t('native_tools')}
              </label>
              <p className="text-fl-muted-2 text-sm">{t('capabilitiesHelp')}</p>
            </details>
            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                className={buttonClass}
                disabled={!options.model.trim()}
                onClick={() => void run(async () => {
                  const result = await request('/test', 'POST', payload())
                  setMessage(t('testResult', { model: result.model, answer: result.answer }))
                })}
              >
                {t('test')}
              </button>
              <button type="submit" className={buttonClass}>
                {t('save')}
              </button>
            </div>
            <p className="text-fl-muted-2 text-sm">{t('saveHelp')}</p>
          </fieldset>
        </form>
      )}
    </div>
  )
}
