import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, errorMessage } from '../api/client';
import type { CommandRequest, CommandResponse, ControlState, Message, Notification } from '../api/types';
import { useInterval } from '../lib/hooks';
import { localTz } from '../lib/format';
import { loadVoicePrefs, saveVoicePrefs, tts, type VoicePrefs } from '../voice/tts';

// ---------------- Control (Pause actions) ----------------
interface ControlCtx {
  control: ControlState | null;
  error: string | null;
  busy: boolean;
  pause: (reason?: string) => Promise<void>;
  resume: () => Promise<void>;
  refresh: () => Promise<void>;
}
const ControlContext = createContext<ControlCtx | null>(null);

function useControlState(): ControlCtx {
  const [control, setControl] = useState<ControlState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try {
      setControl(await api.control());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useInterval(() => void refresh(), 5000);
  const pause = useCallback(async (reason?: string) => {
    setBusy(true);
    try {
      setControl(await api.pause(reason));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, []);
  const resume = useCallback(async () => {
    setBusy(true);
    try {
      setControl(await api.resume());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, []);
  return { control, error, busy, pause, resume, refresh };
}

// ---------------- Notifications ----------------
interface NotificationsCtx {
  items: Notification[];
  unread: number;
  error: string | null;
  refresh: () => Promise<void>;
  markRead: (id: string) => Promise<void>;
}
const NotificationsContext = createContext<NotificationsCtx | null>(null);

function useNotificationsState(): NotificationsCtx {
  const [items, setItems] = useState<Notification[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seen = useRef<Set<string> | null>(null);
  const refresh = useCallback(async () => {
    try {
      const list = await api.notifications();
      setItems(list);
      setError(null);
      // Opt-in local browser notification for newly arrived items while the tab is hidden.
      if (seen.current) {
        const fresh = list.filter((n) => !n.read_at && !seen.current!.has(n.id));
        if (fresh.length && typeof Notification !== 'undefined' && Notification.permission === 'granted' && document.visibilityState === 'hidden') {
          for (const n of fresh.slice(0, 3)) {
            try {
              new Notification(n.title, { tag: n.id });
            } catch {
              /* some browsers require SW notifications */
            }
          }
        }
      }
      seen.current = new Set(list.map((n) => n.id));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useInterval(() => void refresh(), 30000);
  const markRead = useCallback(
    async (id: string) => {
      await api.markNotificationRead(id);
      await refresh();
    },
    [refresh],
  );
  const unread = items.filter((n) => !n.read_at).length;
  return { items, unread, error, refresh, markRead };
}

// ---------------- Speech ----------------
interface SpeechCtx {
  prefs: VoicePrefs;
  setPrefs: (p: Partial<VoicePrefs>) => void;
  speaking: boolean;
  lastSpoken: string | null;
  speak: (text: string | null | undefined) => void;
  stop: () => void;
  replay: () => void;
  available: boolean;
}
const SpeechContext = createContext<SpeechCtx | null>(null);

function useSpeechState(): SpeechCtx {
  const [prefs, setPrefsState] = useState<VoicePrefs>(() => loadVoicePrefs());
  const [speaking, setSpeaking] = useState(false);
  const [lastSpoken, setLastSpoken] = useState<string | null>(null);
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;
  useEffect(() => tts.subscribe(() => setSpeaking(tts.isSpeaking())), []);
  const setPrefs = useCallback((p: Partial<VoicePrefs>) => {
    setPrefsState((prev) => {
      const next = { ...prev, ...p };
      saveVoicePrefs(next);
      if (next.muted) tts.stop();
      // Keep the server-side default in sync (best effort; per-device choice lives in localStorage).
      void api
        .patchSettings({ voice: { voice_name: next.voiceName, rate: next.rate, muted: next.muted, auto_speak: !next.muted } })
        .catch(() => undefined);
      return next;
    });
  }, []);
  const speak = useCallback((text: string | null | undefined) => {
    if (!text) return;
    setLastSpoken(text);
    if (prefsRef.current.muted) return;
    tts.speak(text, { voiceName: prefsRef.current.voiceName, rate: prefsRef.current.rate });
  }, []);
  const stop = useCallback(() => tts.stop(), []);
  const replay = useCallback(() => {
    if (lastSpoken) tts.speak(lastSpoken, { voiceName: prefsRef.current.voiceName, rate: prefsRef.current.rate });
  }, [lastSpoken]);
  return { prefs, setPrefs, speaking, lastSpoken, speak, stop, replay, available: tts.available };
}

// ---------------- Conversation / console thread ----------------
export type ThreadEntry =
  | { kind: 'user'; id: string; text: string; channel: 'voice' | 'typed'; at: string }
  | { kind: 'response'; id: string; resp: CommandResponse }
  | { kind: 'history'; id: string; message: Message }
  | { kind: 'error'; id: string; text: string; voice?: boolean };

interface ConversationCtx {
  conversationId: string | null;
  ensureConversation: () => Promise<string>;
  thread: ThreadEntry[];
  sending: boolean;
  send: (req: Omit<CommandRequest, 'conversation_id' | 'tz'> & { tz?: string }) => Promise<CommandResponse | null>;
  pushEntry: (e: ThreadEntry) => void;
  updateResponse: (runId: string, fn: (r: CommandResponse) => CommandResponse) => void;
  newConversation: () => Promise<void>;
  loadError: string | null;
}
const ConversationContext = createContext<ConversationCtx | null>(null);
const CONV_KEY = 'jarvis.conversationId';

let idCounter = 0;
export const localId = (p: string) => `${p}-${Date.now()}-${++idCounter}`;

function useConversationState(speak: (t: string | null | undefined) => void): ConversationCtx {
  const [conversationId, setConversationId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(CONV_KEY);
    } catch {
      return null;
    }
  });
  const [thread, setThread] = useState<ThreadEntry[]>([]);
  const [sending, setSending] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const pending = useRef<Promise<string> | null>(null);

  useEffect(() => {
    if (!conversationId) return;
    api
      .messages(conversationId)
      .then((msgs) => {
        setThread((t) => (t.length ? t : msgs.map((m) => ({ kind: 'history' as const, id: m.id, message: m }))));
        setLoadError(null);
      })
      .catch((e) => setLoadError(`Could not load earlier messages: ${errorMessage(e)}`));
    // only on first mount / id change
  }, [conversationId]);

  const ensureConversation = useCallback(async () => {
    if (conversationId) return conversationId;
    if (!pending.current) {
      pending.current = api.createConversation().then((r) => {
        try {
          sessionStorage.setItem(CONV_KEY, r.id);
        } catch {
          /* ignore */
        }
        setConversationId(r.id);
        return r.id;
      });
      pending.current.catch(() => (pending.current = null));
    }
    return pending.current;
  }, [conversationId]);

  const pushEntry = useCallback((e: ThreadEntry) => setThread((t) => [...t, e]), []);

  const send = useCallback<ConversationCtx['send']>(
    async (req) => {
      setSending(true);
      pushEntry({ kind: 'user', id: localId('u'), text: req.text, channel: req.channel, at: req.captured_at });
      try {
        const conversation_id = await ensureConversation();
        const resp = await api.command({ ...req, tz: req.tz ?? localTz(), conversation_id });
        pushEntry({ kind: 'response', id: resp.run_id || localId('r'), resp });
        speak(resp.message?.spoken_text);
        return resp;
      } catch (e) {
        pushEntry({ kind: 'error', id: localId('e'), text: errorMessage(e) });
        return null;
      } finally {
        setSending(false);
      }
    },
    [ensureConversation, pushEntry, speak],
  );

  const updateResponse = useCallback((runId: string, fn: (r: CommandResponse) => CommandResponse) => {
    setThread((t) => t.map((e) => (e.kind === 'response' && e.resp.run_id === runId ? { ...e, resp: fn(e.resp) } : e)));
  }, []);

  const newConversation = useCallback(async () => {
    try {
      sessionStorage.removeItem(CONV_KEY);
    } catch {
      /* ignore */
    }
    pending.current = null;
    setConversationId(null);
    setThread([]);
  }, []);

  return { conversationId, ensureConversation, thread, sending, send, pushEntry, updateResponse, newConversation, loadError };
}

// ---------------- Provider ----------------
export function AppStateProvider({ children }: { children: ReactNode }) {
  const control = useControlState();
  const notifications = useNotificationsState();
  const speech = useSpeechState();
  const conversation = useConversationState(speech.speak);
  return (
    <ControlContext.Provider value={control}>
      <NotificationsContext.Provider value={notifications}>
        <SpeechContext.Provider value={speech}>
          <ConversationContext.Provider value={conversation}>{children}</ConversationContext.Provider>
        </SpeechContext.Provider>
      </NotificationsContext.Provider>
    </ControlContext.Provider>
  );
}

function must<T>(v: T | null, name: string): T {
  if (!v) throw new Error(`${name} used outside AppStateProvider`);
  return v;
}
export const useControl = () => must(useContext(ControlContext), 'useControl');
export const useNotifications = () => must(useContext(NotificationsContext), 'useNotifications');
export const useSpeech = () => must(useContext(SpeechContext), 'useSpeech');
export const useConversation = () => must(useContext(ConversationContext), 'useConversation');

export function useIsPaused(): boolean {
  const { control } = useControl();
  return useMemo(() => !!control?.actions_paused, [control]);
}
