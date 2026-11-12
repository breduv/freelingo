import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  user: { id: 1, role: 'admin' },
}))

vi.mock('@/lib/api', () => ({ apiFetch: mocks.apiFetch }))
vi.mock('@/store/auth', () => ({
  useAuthStore: (selector: (state: { user: typeof mocks.user }) => unknown) => selector({ user: mocks.user }),
}))
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }))
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}))

import AiSettingsPage from '@/app/(app)/settings/ai/page'

const config = {
  options: {
    provider: 'ollama',
    api_base: 'http://ollama.test',
    model: 'local-model',
    timeout_seconds: 120,
    max_output_tokens: null,
    temperature: null,
    reasoning_effort: '',
    native_tools: true,
    verify_lessons: true,
  },
  provider_defaults: Object.fromEntries(
    ['ollama', 'openai', 'anthropic', 'deepseek', 'chatgpt'].map((provider) => [
      provider,
      {
        options: {
          provider,
          api_base: provider === 'ollama' ? 'http://ollama.test' : 'https://api.example',
          model: `${provider}-model`,
          timeout_seconds: 120,
          max_output_tokens: provider === 'anthropic' ? 8192 : null,
          temperature: null,
          reasoning_effort: '',
          native_tools: true,
          verify_lessons: true,
        },
        has_api_key: false,
      },
    ])
  ),
  has_api_key: true,
  source: 'saved',
  chatgpt: { connected: false, plan_enabled: false, email: '' },
}

function response(data: unknown) {
  return { ok: true, json: async () => structuredClone(data) }
}

describe('AI settings', () => {
  beforeEach(() => {
    mocks.user = { id: 1, role: 'admin' }
    mocks.apiFetch.mockReset()
    mocks.apiFetch.mockResolvedValue(response(config))
  })

  it('keeps saved keys out of the form and sends the selected endpoint for testing without saving', async () => {
    render(<AiSettingsPage />)
    expect(await screen.findByLabelText('API_KEY')).toHaveValue('')
    await waitFor(() => expect(screen.getByRole('button', { name: 'test' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('OLLAMA_BASE_URL'), { target: { value: 'https://new.example/v1' } })
    mocks.apiFetch.mockResolvedValueOnce(response({ model: 'local-model', answer: 'READY' }))
    fireEvent.click(screen.getByRole('button', { name: 'test' }))
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledTimes(2))
    const [path, options] = mocks.apiFetch.mock.calls[1]
    expect(path).toBe('/api/admin/llm/test')
    expect(JSON.parse(options.body)).toMatchObject({ api_base: 'https://new.example/v1', api_key: '' })
    expect(mocks.apiFetch.mock.calls.some(([, request]) => request.method === 'PUT')).toBe(false)
  })

  it('does not request installation settings for a learner', () => {
    mocks.user = { id: 2, role: 'user' }
    render(<AiSettingsPage />)
    expect(screen.getByText('adminOnly')).toBeInTheDocument()
    expect(mocks.apiFetch).not.toHaveBeenCalled()
  })

  it('offers the original providers and uses provider-specific defaults', async () => {
    render(<AiSettingsPage />)
    const provider = await screen.findByLabelText('source')
    await waitFor(() => expect(provider).toBeEnabled())
    expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic / Claude' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'DeepSeek' })).toBeInTheDocument()
    fireEvent.change(provider, { target: { value: 'anthropic' } })
    expect(screen.getByLabelText('model')).toHaveValue('anthropic-model')
    expect(screen.queryByLabelText('OLLAMA_BASE_URL')).not.toBeInTheDocument()
    expect(screen.getByLabelText('outputTokens')).toHaveValue(8192)
  })

  it('cancels pending configuration loading when leaving the page', () => {
    mocks.apiFetch.mockReturnValue(new Promise(() => {}))
    const page = render(<AiSettingsPage />)
    const signal = mocks.apiFetch.mock.calls[0][1].signal as AbortSignal
    page.unmount()
    expect(signal.aborted).toBe(true)
  })

  it('keeps the form recoverable after a failed save', async () => {
    render(<AiSettingsPage />)
    await screen.findByLabelText('OLLAMA_BASE_URL')
    await waitFor(() => expect(screen.getByRole('button', { name: 'save' })).toBeEnabled())
    mocks.apiFetch.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(screen.getByRole('button', { name: 'save' }))
    expect(await screen.findByText('offline')).toBeInTheDocument()
    expect(screen.getByLabelText('OLLAMA_BASE_URL')).toHaveValue('http://ollama.test')
    expect(screen.getByRole('button', { name: 'save' })).toBeEnabled()
  })
})
