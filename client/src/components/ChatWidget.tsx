import { useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'

// A floating chat in the bottom-right corner. Questions go to the ERP chatbot (POST /chat);
// the session id is kept so follow-up questions have context. Detailed design comes later.

type Msg = { role: 'user' | 'assistant'; text: string; tools?: string[] }

const SUGGESTIONS = ["What are Meridian's payment terms?", "Which open orders are over the customer's credit limit?", 'Which orders are on hold, and why?']

export function ChatWidget() {
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [session, setSession] = useState<string | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => bottom.current?.scrollIntoView({ behavior: 'smooth' }), [msgs, busy])

  async function send(text: string) {
    const q = text.trim()
    if (!q || busy) return
    setInput('')
    setMsgs((m) => [...m, { role: 'user', text: q }])
    setBusy(true)
    try {
      const r = await api.chat(q, session)
      setSession(r.session_id)
      setMsgs((m) => [...m, { role: 'assistant', text: r.answer, tools: r.tools.map((t) => t.tool) }])
    } catch (e) {
      setMsgs((m) => [...m, { role: 'assistant', text: `Sorry, that failed: ${(e as Error).message}` }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed bottom-5 right-5 z-50 flex flex-col items-end gap-3">
      {open && (
        <div className="flex h-[34rem] w-[26rem] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
          <header className="flex items-center justify-between border-b border-slate-100 bg-slate-900 px-4 py-3 text-white">
            <div>
              <p className="text-sm font-semibold">Ask the ERP</p>
              <p className="text-xs text-slate-300">Answers come from the ERP and the agent's checks</p>
            </div>
            <button
              onClick={() => {
                setMsgs([])
                setSession(null)
              }}
              className="text-xs text-slate-300 hover:text-white"
            >
              New chat
            </button>
          </header>
          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {!msgs.length && (
              <div className="space-y-2">
                <p className="text-sm text-slate-500">Try:</p>
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => send(s)} className="block w-full rounded-lg border border-slate-200 px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50">
                    {s}
                  </button>
                ))}
              </div>
            )}
            {msgs.map((m, i) =>
              m.role === 'user' ? (
                <div key={i} className="ml-8 rounded-2xl rounded-br-sm bg-slate-900 px-3 py-2 text-sm text-white">{m.text}</div>
              ) : (
                <div key={i} className="mr-4">
                  <div className="prose prose-sm max-w-none rounded-2xl rounded-bl-sm bg-slate-100 px-3 py-2 prose-table:text-xs prose-th:px-1 prose-td:px-1">
                    <Markdown remarkPlugins={[remarkGfm]}>{m.text}</Markdown>
                  </div>
                  {m.tools && m.tools.length > 0 && <p className="mt-1 px-1 text-[11px] text-slate-400">used: {[...new Set(m.tools)].join(', ')}</p>}
                </div>
              ),
            )}
            {busy && <p className="text-sm text-slate-400">Looking it up…</p>}
            <div ref={bottom} />
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              send(input)
            }}
            className="flex gap-2 border-t border-slate-100 p-3"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about customers, orders, credit, stock…"
              className="flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-slate-400"
            />
            <button disabled={busy || !input.trim()} className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40">
              Send
            </button>
          </form>
        </div>
      )}
      <button
        onClick={() => setOpen(!open)}
        aria-label={open ? 'Close chat' : 'Open chat'}
        className="flex h-14 w-14 items-center justify-center rounded-full bg-slate-900 text-white shadow-lg hover:bg-slate-800"
      >
        {open ? (
          <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 6l12 12M18 6L6 18" /></svg>
        ) : (
          <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z" /></svg>
        )}
      </button>
    </div>
  )
}
