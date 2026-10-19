"""Emit va_client phase messages from Pipecat speaking frames.

The Home Assistant Voice PE firmware (maxmaxme `va_client` component) drives its
LED ring, mic-streaming gate and a 7 s no-speech watchdog from `phase` JSON
messages sent by the backend:

    {"type": "phase", "value": "listening" | "thinking" | "replying" | "idle"}

Without these messages the device aborts each turn after the watchdog fires, so
emitting them is required (not just cosmetic). This processor maps Pipecat's
standard speaking frames onto those phases and forwards them to the device over
the websocket as TEXT frames.

Mapping:
    UserStartedSpeakingFrame  -> listening   (server VAD heard the user)
    UserStoppedSpeakingFrame  -> thinking    (generating a response)
    BotStartedSpeakingFrame   -> replying    (TTS audio is playing)
    BotStoppedSpeakingFrame   -> idle, but DEBOUNCED (see below)

IMPORTANT — idle debounce:
    OpenAI Realtime TTS arrives in segments (per sentence, and around tool
    calls), so BotStoppedSpeakingFrame fires several times *within a single
    reply*, with sub-second gaps before the next BotStartedSpeakingFrame. If we
    emitted "idle" on every BotStoppedSpeakingFrame the device's LED would flap
    replying -> idle -> replying mid-answer, and — because the firmware only
    arms the "stop" wake word during "replying" — the user would briefly lose
    the ability to interrupt. So we do NOT go idle immediately on BotStopped:
    we arm a short timer and only emit "idle" if no further bot/user speech
    starts before it elapses (i.e. the reply has truly finished). Any
    Bot/UserStartedSpeaking cancels the pending idle.

A barge-in mid-reply surfaces as a fresh UserStartedSpeakingFrame -> "listening"
(which cancels the pending idle); the firmware uses that to flush playback.

IMPORTANT — thinking watchdog + forced idle (v0.5.3):
    `thinking` is the one phase with no natural exit when a turn dies without
    a reply: a rate-limited / failed response.create produces no Bot frames,
    so the device blinks "thinking" forever WITH AN OPEN MIC (observed live
    2026-06-12: 44 s stuck, during which the mic picked up unrelated talking
    and the model answered it). Two defenses here:

    1. `force_idle(reason)` — the turn-death paths (ConnectionRecovery's
       rate-limit unstick + reconnect) call this INSTEAD of broadcasting idle
       around this processor, so the internal state stays consistent, AND it
       suppresses subsequent `thinking` emissions until real activity (user
       or bot speech) follows. Without the suppression, a VAD stop event
       already in flight re-emits `thinking` right after the unstick idle —
       the exact 400 ms race observed.
    2. A thinking watchdog — if `thinking` sees no model activity for
       THINKING_TIMEOUT_S it forces idle as a generic safety net (covers
       turn deaths that produce no ErrorFrame at all). While a tool call is
       in flight (TURN_LIVENESS; tool handlers are wrapped in
       SafeRealtimeLLMService.register_function to tick it) the watchdog
       WAITS WITH NO CAP — explicit user decision 2026-06-12: a long web
       search on a hard question must get all the time it needs, the user
       knowingly waits. This cannot wait forever: every tool is bounded by
       its own client timeout (MCP ~30 s HTTP; web search the OpenAI
       client's 600 s default, whose except-path feeds the model a spoken
       error), and the wrapper's `finally` guarantees in_flight always
       drops back to 0 — after which the normal THINKING_TIMEOUT_S window
       applies again. If a slow-but-alive turn is ever cut off, the late
       reply still plays (BotStarted -> replying) — degraded but never stuck.
"""
import asyncio
import logging
import os
import time

from pipecat.frames.frames import (
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

logger = logging.getLogger(__name__)


class TurnLiveness:
    """Shared "is the model still doing something?" signal for the watchdog.

    Tool handlers are wrapped (see SafeRealtimeLLMService.register_function in
    main.py) to tick this on start/finish. The PhaseEmitter's thinking
    watchdog reads it so a slow tool — web search regularly takes 10-20 s with
    zero pipeline traffic — is never mistaken for a dead turn, and so each
    step of a long tool chain refreshes the window. Module-level singleton:
    one pipeline per process.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.last_activity = 0.0
        # Total tools ever started; lets PhaseEmitter tell whether a tool ran
        # during the current response (so another response will follow it).
        self.started = 0

    def tool_started(self) -> None:
        self.in_flight += 1
        self.started += 1
        self.last_activity = time.monotonic()

    def tool_finished(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.last_activity = time.monotonic()


TURN_LIVENESS = TurnLiveness()


class StaleAudio:
    """Set while the device's mic is closed (after a device interrupt or a
    follow-up timeout) and the server VAD nevertheless reports speech. That
    speech is late audio from a turn the device already gave up on — e.g. the
    no-speech watchdog fired because OpenAI's VAD lagged — so it must not
    become a turn. Tools check this and refuse to run; responses are cancelled.
    Cleared on the next wake. Module-level singleton like TURN_LIVENESS."""

    def __init__(self) -> None:
        self.active = False


STALE_AUDIO = StaleAudio()


class PhaseEmitter(FrameProcessor):
    """Forwards phase transitions to the device as JSON text frames."""

    # Thinking watchdog: how long `thinking` may sit without any model
    # activity before we declare the turn dead and force idle. Normal silent
    # gaps (turn end -> first token, tool result -> next response) are 1-4 s,
    # so 15 s has ample margin without leaving the user staring at a blinking
    # LED for long. While a tool is in flight the watchdog waits with NO cap
    # (see the module docstring — explicit user decision).
    THINKING_TIMEOUT_S = 15.0
    WATCHDOG_POLL_S = 1.0
    # How often to log that we're deliberately waiting on a running tool.
    INFLIGHT_LOG_EVERY_S = 30.0
    # A voice spoken by our own TTS (ElevenLabs, the second persona) arrives sentence by
    # sentence with pauses of well over half a second between them (0.65 s observed), so
    # its turn only counts as over after this much silence: going idle at the first pause
    # chimed the device's mic open, then the rest of the reply closed it again on the
    # user's first words.
    TTS_SENTENCE_GAP_S = 1.0

    def __init__(self, send_phase, idle_debounce_s: float = None, request_follow_up=None, **kwargs):
        """
        Args:
            send_phase: async callable(value: str) that delivers the phase to
                the connected device(s).
            idle_debounce_s: seconds the bot must stay silent after a reply
                before we declare the turn idle. Defaults to the
                PHASE_IDLE_DEBOUNCE_MS env var (1500 ms) — long enough to bridge
                the inter-sentence / tool-call gaps in OpenAI Realtime TTS so the
                LED and the "stop" wake word stay active for the whole answer.
        """
        super().__init__(**kwargs)
        self._send_phase = send_phase
        if idle_debounce_s is None:
            try:
                idle_debounce_s = float(os.environ.get("PHASE_IDLE_DEBOUNCE_MS", "1500")) / 1000.0
            except (TypeError, ValueError):
                idle_debounce_s = 1.5
        self._idle_debounce_s = max(0.0, idle_debounce_s)
        self._idle_task = None
        self._watchdog_task = None
        self._current = None  # last phase actually sent, to dedupe redundant emits
        # Set by force_idle(): the turn was declared dead, so a VAD stop event
        # that is still in flight must NOT re-emit `thinking` and re-stick the
        # device. Cleared on the next real activity (user/bot speech start).
        self._suppress_thinking = False
        # Dangling-VAD guard (A). The device sends {"type":"wake"} on every wake;
        # note_wake() resets this to False. A real UserStartedSpeaking sets it
        # True. A UserStoppedSpeaking with this still False is a server-VAD
        # segment from a PREVIOUS turn closing late (the reply gated the mic mid-
        # utterance, so the VAD never saw the stop) — committing it auto-creates
        # a garbage response to an empty turn. We then suppress the thinking and
        # cancel that racing response via the kill-window callback. Defaults True
        # so nothing is suppressed before the first wake signal (and so old
        # firmware that doesn't send `wake` degrades to a no-op).
        self._speech_since_wake = True
        # Callbacks into the websocket_handler's kill-window (set after the
        # _interrupt_kill_until dict exists). _on_dangling_stop arms it (cancel
        # the dangling turn's racing response); _on_real_speech clears it (a
        # genuine new utterance — never cancel ITS response).
        self._on_dangling_stop = None
        self._on_real_speech = None
        # Question-only follow-up: async callable that asks the device to open
        # the mic once more (va_client's {"type":"request_follow_up"}). Set only
        # when the timed follow-up window is off; fired after the debounced idle
        # when the last reply ended with a question mark.
        self._request_follow_up = request_follow_up
        self._last_reply_question = False
        # When the last follow-up was asked for. A voice that comes sentence by sentence
        # (ElevenLabs) can pause long enough to look finished: if more of the reply
        # plays shortly after, that request was closed again by `replying`, so it's
        # asked for once more when the reply really ends.
        self._follow_up_asked_at = None
        # Set when the user woke the device while a reply was still coming out (barge_in):
        # the rest of that reply is dropped on its way to the device and its speaking
        # start/stop no longer moves the phase, until it has finished or a new reply starts.
        self._barged = False
        # Reply-complete tracking, fed by the assistant TranscriptLogger tap
        # (response start/end brackets). Lets BotStoppedSpeaking skip the idle
        # debounce once the final reply of a turn has finished.
        self._response_open = False
        self._reply_done = False
        self._tools_at_response_start = 0
        # True while the device's mic is closed (interrupt / follow-up timeout)
        # until the next wake. Speech the VAD reports meanwhile is stale.
        self._device_mic_closed = False
        self._on_stale_speech = None
        # Between TTSStarted and TTSStopped audio is still being produced. For
        # the ElevenLabs persona the LLM text finishes long before the speech,
        # so a gap between sentences must not end the turn.
        self._tts_active = False

    def note_device_mic_closed(self) -> None:
        """The device closed its mic (interrupt or follow-up timeout)."""
        self._device_mic_closed = True

    def note_device_mic_opened(self) -> None:
        """The device woke and opened its mic for a fresh turn."""
        self._device_mic_closed = False
        STALE_AUDIO.active = False

    def set_stale_speech_handler(self, handler) -> None:
        """Async callable run when speech starts while the device mic is closed."""
        self._on_stale_speech = handler

    @property
    def barged(self) -> bool:
        """True while what's left of an interrupted reply is to be dropped."""
        return self._barged

    async def barge_in(self) -> bool:
        """The device woke while a reply was still playing or being worked out: the
        user is taking the turn. Open the device's mic now (`listening`), forget the
        reply's follow-up, and let the rest of its audio drain unheard. Without this the
        phase stayed `replying` (device mic gated, the user's words lost) until the
        reply's audio ended, and its idle + follow-up then chimed in mid-sentence."""
        if self._current not in ("replying", "thinking"):
            return False
        self._barged = True
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._last_reply_question = False
        self._follow_up_asked_at = None
        self._reply_done = False
        logger.info("👋 woken mid-reply → the user's turn now (rest of the reply dropped)")
        await self._emit("listening")
        return True

    def note_response_start(self) -> None:
        """An assistant response began (LLMFullResponseStartFrame)."""
        self._barged = False
        self._response_open = True
        self._reply_done = False
        self._last_reply_question = False
        self._tools_at_response_start = TURN_LIVENESS.started

    def note_response_end(self) -> None:
        """The assistant response finished generating (LLMFullResponseEndFrame)."""
        self._response_open = False
        self._reply_done = True

    def _turn_is_over(self) -> bool:
        """True when nothing more will be said this turn: the last response
        has finished, no new one has started, and no tool ran during it (a tool
        call means OpenAI will follow up with another response)."""
        from app.persona import STATE  # late import: persona imports pipecat frames too
        return (self._reply_done and not self._response_open
                and not (STATE.speaks_with_tts and self._tts_active)
                and TURN_LIVENESS.in_flight == 0
                and TURN_LIVENESS.started == self._tools_at_response_start)

    def note_assistant_reply(self, text: str) -> None:
        """Remember whether the latest reply ended with a question, so the turn's
        idle can ask the device to listen for the answer."""
        # Ignore trailing quotes/brackets, e.g. a reply ending ?" or ?)
        self._last_reply_question = text.rstrip(" \"'”’)").endswith("?")

    def note_wake(self) -> None:
        """Device woke (or a follow-up window closed without speech). Until the
        next real UserStartedSpeaking, any UserStoppedSpeaking is a dangling
        pre-wake VAD segment (see _speech_since_wake)."""
        self._speech_since_wake = False

    def set_kill_window_handlers(self, on_dangling=None, on_real_speech=None) -> None:
        """Wire the dangling-VAD guard to the websocket_handler kill-window."""
        self._on_dangling_stop = on_dangling
        self._on_real_speech = on_real_speech

    async def force_idle(self, reason: str = "") -> None:
        """Declare the current turn dead and put the device in idle.

        Used by the turn-death paths (rate-limit unstick, reconnect,
        thinking watchdog). Goes through the normal emit so the internal
        state stays consistent, and suppresses `thinking` until real
        activity follows — see the module docstring for the race this
        prevents.
        """
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._suppress_thinking = True
        if reason:
            logger.warning(f"📞 forcing phase idle ({reason[:90]})")
        await self._emit("idle")

    async def _emit(self, value: str) -> None:
        # "listening" is NEVER deduped. The device lifts its post-stop incoming-
        # audio suppression ONLY on receiving a "listening" phase (firmware
        # 14bff74). A stop can RE-SET that suppression after our last "listening"
        # without us emitting a different phase in between, so _current=="listening"
        # no longer reflects the device's suppress state — deduping the next real
        # turn's "listening" then leaves the device muted and the reply is dropped
        # (observed live 2026-06-14: rapid stop/wake testing → web-search answer
        # silently suppressed). A redundant "listening" is idempotent on the
        # device (re-lifts suppress, re-opens the mic gate; the barge-in cut-over
        # is a no-op because the mic is gated during a reply so a real
        # UserStartedSpeaking never coincides with queued TTS).
        if value == self._current and value != "listening":
            return
        self._current = value
        logger.info(f"📞 phase -> {value}")  # TEMP instrumentation
        if self._send_phase is not None:
            try:
                await self._send_phase(value)
            except Exception as e:  # never let UI signalling break the audio path
                logger.warning(f"⚠️ Failed to emit phase '{value}': {e}")

    def _cancel_pending_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._thinking_watchdog())

    async def _emit_idle_after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._idle_debounce_s)
        except asyncio.CancelledError:
            return
        # A tool (web search, MCP call) can still be running when the filler
        # reply's debounce expires — the turn isn't over, the model is
        # "thinking" while it waits for the tool. Going idle here makes the
        # device look done (idle LED, and it opens a follow-up window) while it
        # is actually still working — confusing on a slow web search. Show
        # `thinking` instead and arm the watchdog (which waits without a cap
        # while a tool is in flight); the tool's result response then flips the
        # phase to `replying`. Fast tools never reach here — their result reply
        # cancels this debounce first.
        # Same when a tool ran during the last response and has just finished:
        # OpenAI is about to answer with its result, so the turn isn't over.
        if TURN_LIVENESS.in_flight > 0 or (
                TURN_LIVENESS.started != self._tools_at_response_start
                and not self._response_open):
            await self._emit("thinking")
            self._arm_watchdog()
            return
        await self._finish_turn()

    async def _finish_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self._finish_turn()

    async def _finish_turn(self) -> None:
        """Go idle, then ask the device to listen for an answer if the reply
        ended with a question."""
        self._reply_done = False
        await self._emit("idle")
        if self._last_reply_question and self._request_follow_up is not None:
            self._last_reply_question = False
            self._follow_up_asked_at = time.monotonic()
            logger.info("❓ reply ended with a question → request_follow_up")
            try:
                await self._request_follow_up()
            except Exception as e:
                logger.warning(f"⚠️ Failed to request follow-up: {e}")

    async def _thinking_watchdog(self) -> None:
        """Force idle when `thinking` sits with no model activity (dead turn)."""
        armed_at = time.monotonic()
        last_inflight_log = 0.0
        try:
            while True:
                await asyncio.sleep(self.WATCHDOG_POLL_S)
                if self._current != "thinking":
                    return  # phase moved on — turn is alive, watchdog done
                now = time.monotonic()
                last = max(armed_at, TURN_LIVENESS.last_activity)
                if TURN_LIVENESS.in_flight > 0:
                    # A tool is running — the turn is alive by definition, and
                    # a long web search must get all the time it needs (no
                    # cap; see the module docstring). Log occasionally so a
                    # long wait is visibly deliberate in the log.
                    if now - last_inflight_log >= self.INFLIGHT_LOG_EVERY_S:
                        last_inflight_log = now
                        logger.info(
                            f"⏳ thinking-watchdog: {TURN_LIVENESS.in_flight} tool(s) "
                            f"running for {now - last:.0f}s — waiting (no cap)"
                        )
                    continue
                if now - last < self.THINKING_TIMEOUT_S:
                    continue
                logger.warning(
                    f"⚠️ thinking-watchdog: no model activity for {now - last:.0f}s "
                    f"and no tool in flight — forcing idle to unstick the device"
                )
                await self.force_idle("thinking-watchdog")
                return
        except asyncio.CancelledError:
            return

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame) and self._device_mic_closed:
            # Late audio: the device already closed its mic for this turn.
            # Don't emit `listening` (that would reopen the device mic) and
            # don't let it become a turn.
            STALE_AUDIO.active = True
            logger.info("📞 speech reported after the device closed its mic — ignored (late audio)")
            if self._on_stale_speech is not None:
                try:
                    await self._on_stale_speech()
                except Exception as e:
                    logger.warning(f"⚠️ stale-speech handler failed: {e!r}")
        elif isinstance(frame, UserStoppedSpeakingFrame) and STALE_AUDIO.active:
            logger.info("📞 'thinking' suppressed — late audio after the device closed its mic")
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._suppress_thinking = False
            self._last_reply_question = False
            self._follow_up_asked_at = None
            self._reply_done = False
            # A: a genuine utterance has begun this turn → not a dangling VAD,
            # and the kill-window must NOT cancel THIS turn's response.
            self._speech_since_wake = True
            if self._on_real_speech is not None:
                self._on_real_speech()
            self._cancel_pending_idle()
            self._cancel_watchdog()
            await self._emit("listening")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._cancel_pending_idle()
            if self._current == "replying":
                # C: the bot is already replying. With barge_in:false the mic is
                # gated during a reply, so a user-speech-stop here can only be a
                # stale VAD tail of the question that just got its reply (the VAD
                # split the utterance and the second half closed late). Emitting
                # `thinking` would overwrite `replying`, reopen the mic mid-reply
                # (the TTS leaks in) and strand the LED in `thinking` until the
                # 15 s watchdog. Keep replying.
                logger.info("📞 'thinking' suppressed — bot is replying (stale VAD tail)")
            elif not self._speech_since_wake:
                # A: no real speech since the last wake/flush → this stop is a
                # dangling pre-wake server-VAD segment closing late. Suppress the
                # thinking AND cancel the garbage response the server auto-creates
                # for the (empty) committed turn.
                logger.info("📞 'thinking' suppressed + kill armed — dangling VAD (no speech since wake)")
                if self._on_dangling_stop is not None:
                    self._on_dangling_stop()
            elif self._suppress_thinking:
                # A VAD stop raced a turn-death force_idle — stay idle.
                logger.info("📞 phase 'thinking' suppressed (turn already declared dead)")
            else:
                await self._emit("thinking")
                self._arm_watchdog()
        elif isinstance(frame, TTSStartedFrame):
            self._tts_active = True
        elif isinstance(frame, TTSStoppedFrame):
            self._tts_active = False
        elif isinstance(frame, BotStartedSpeakingFrame) and self._barged:
            pass   # the interrupted reply's leftover audio: not a reply to show
        elif isinstance(frame, BotStoppedSpeakingFrame) and self._barged:
            self._barged = False   # it has drained; the next speech is a real reply
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._suppress_thinking = False
            if self._follow_up_asked_at is not None and time.monotonic() - self._follow_up_asked_at < 3.0:
                self._last_reply_question = True
                logger.info("❓ more of the reply after the follow-up request → asking again when it ends")
            self._follow_up_asked_at = None
            self._cancel_pending_idle()
            self._cancel_watchdog()
            await self._emit("replying")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._cancel_pending_idle()
            if self._turn_is_over():
                # The final reply has finished and nothing else is coming, so
                # skip the debounce: the LED goes idle (and a question hands
                # over to the user) as soon as the speech actually ends. Our own
                # TTS still gets a moment to show it was only a pause.
                from app.persona import STATE  # late import, as in _turn_is_over
                if STATE.speaks_with_tts:
                    self._idle_task = asyncio.create_task(self._finish_after(self.TTS_SENTENCE_GAP_S))
                else:
                    await self._finish_turn()
            else:
                # Don't go idle immediately — TTS comes in segments. Only emit
                # idle if the bot stays silent for the debounce window.
                self._idle_task = asyncio.create_task(self._emit_idle_after_debounce())

        await self.push_frame(frame, direction)
