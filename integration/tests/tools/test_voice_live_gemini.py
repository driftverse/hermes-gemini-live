"""Gemini Live provider for the app's Live mode: model chain, cooldown, status, setup message.

stdlib unittest on purpose (also collected by pytest). No network: the token mint is patched.
"""

import unittest
from unittest import mock

from tools import voice_live as vl


def _voice(**gemini):
    return {"voice_chat_mode": "gpt-live", "gpt_live": {"provider": "gemini", "gemini": gemini}}


class GeminiChain(unittest.TestCase):
    def setUp(self):
        vl._cooldowns.clear()
        self.minted = mock.patch.object(vl, "_mint_gemini_token", return_value="tok123").start()
        self.key = mock.patch.object(vl, "_resolve_gemini_key", return_value="k").start()
        self.addCleanup(mock.patch.stopall)

    def _session(self, voice, **kwargs):
        with mock.patch.object(vl, "_voice_section", return_value=voice):
            return vl.create_gemini_session(**kwargs)

    def test_default_chain_has_four_delegating_models_and_picks_first(self):
        out = self._session(_voice())
        self.assertEqual(out["session"]["model"], "gemini-3.8-live")
        self.assertEqual(len(vl.DEFAULT_GEMINI_MODELS), 4)
        # Models that were seen not delegating are opt-in only.
        self.assertNotIn("gemini-3.8-live-extended-thinking", vl.DEFAULT_GEMINI_MODELS)
        self.assertNotIn("gemini-2.5-flash-native-audio-preview-09-2025", vl.DEFAULT_GEMINI_MODELS)
        self.assertEqual(out["transport"]["remaining"][-1], "gemini-2.5-flash-native-audio-preview-12-2025")
        self.assertTrue(out["transport"]["url"].endswith("access_token=tok123"))
        self.assertNotIn("api_key", str(out))

    def test_failure_report_moves_to_next_model_and_sets_cooldown(self):
        out = self._session(_voice(), failed=[{"model": "gemini-3.8-live", "reason": "quota"}])
        self.assertEqual(out["session"]["model"], "gemini-3.1-flash-live-preview")
        # A brand-new attempt (no failed list) still skips the cooling model.
        again = self._session(_voice())
        self.assertEqual(again["session"]["model"], "gemini-3.1-flash-live-preview")

    def test_first_three_fail_fourth_answers(self):
        models = list(vl.DEFAULT_GEMINI_MODELS)
        failed = [{"model": m, "reason": "quota"} for m in models[:3]]
        out = self._session(_voice(), failed=failed)
        self.assertEqual(out["session"]["model"], models[3])
        self.assertEqual(out["transport"]["remaining"], [])

    def test_opt_in_thinking_model_gets_a_thinking_level(self):
        out = self._session(_voice(models=["gemini-3.8-live-extended-thinking"], thinking_level="MEDIUM"))
        setup = out["transport"]["setup"]["setup"]
        self.assertEqual(setup["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"})

    def test_all_models_failed_raises_chain_exhausted(self):
        failed = [{"model": m} for m in vl.DEFAULT_GEMINI_MODELS]
        with self.assertRaises(RuntimeError) as ctx:
            self._session(_voice(), failed=failed)
        self.assertIn("Every Gemini Live model failed", str(ctx.exception))

    def test_cooldown_ignored_when_all_cooling_but_exclude_wins(self):
        for m in vl.DEFAULT_GEMINI_MODELS:
            vl.mark_gemini_failure(m)
        # Everything cooling: a fresh attempt still gets the first model rather than nothing.
        self.assertEqual(self._session(_voice())["session"]["model"], "gemini-3.8-live")
        # But a model excluded in this attempt is never re-offered.
        out = self._session(_voice(), failed=[{"model": "gemini-3.8-live"}])
        self.assertNotEqual(out["session"]["model"], "gemini-3.8-live")

    def test_cooldown_expires(self):
        vl.mark_gemini_failure("gemini-3.8-live")
        vl._cooldowns["gemini-3.8-live"] = vl.time.monotonic() - 1
        self.assertEqual(self._session(_voice())["session"]["model"], "gemini-3.8-live")

    def test_resume_pins_same_model_and_carries_handle(self):
        out = self._session(_voice(), model="gemini-3.1-flash-live-preview", resume_handle="h-1")
        self.assertEqual(out["session"]["model"], "gemini-3.1-flash-live-preview")
        self.assertEqual(out["transport"]["setup"]["setup"]["sessionResumption"], {"handle": "h-1"})

    def test_resume_of_unknown_model_is_refused(self):
        with self.assertRaises(RuntimeError):
            self._session(_voice(), model="not-in-chain", resume_handle="h")

    def test_configured_models_and_voice_are_honoured(self):
        out = self._session(_voice(models=["models/a-live", "b-live", "a-live"], voice="Puck"))
        self.assertEqual(out["session"]["model"], "a-live")
        self.assertEqual(out["transport"]["remaining"], ["b-live"])
        voice = out["transport"]["setup"]["setup"]["generationConfig"]["speechConfig"]
        self.assertEqual(voice["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Puck")

    def test_missing_key_and_empty_models(self):
        with mock.patch.object(vl, "_resolve_gemini_key", return_value=""):
            with self.assertRaises(ValueError):
                self._session(_voice())
        with self.assertRaises(ValueError):
            self._session(_voice(models=[]))

    def test_setup_message_is_penny_with_one_delegate_function(self):
        setup = self._session(_voice())["transport"]["setup"]["setup"]
        text = setup["systemInstruction"]["parts"][0]["text"]
        self.assertIn("You are Penny", text)
        self.assertIn("never as Gemini", text)
        decls = setup["tools"][0]["functionDeclarations"]
        self.assertEqual([d["name"] for d in decls], ["delegate_to_hermes"])
        self.assertNotIn("behavior", decls[0])  # blocking: verified on every chain model
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertIn("inputAudioTranscription", setup)
        self.assertIn("outputAudioTranscription", setup)
        self.assertNotIn("thinkingConfig", setup["generationConfig"])

    def test_history_is_folded_into_instructions(self):
        history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello there"}]},
                   {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
        text = self._session(_voice(), history=history)["transport"]["setup"]["setup"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("User: hello there", text)
        self.assertIn("Penny: hi", text)


class Status(unittest.TestCase):
    def test_gemini_status_reports_chain_and_availability(self):
        with mock.patch.object(vl, "_voice_section", return_value=_voice()), \
                mock.patch.object(vl, "_resolve_gemini_key", return_value="k"):
            st = vl.resolve_gpt_live_status()
        self.assertEqual((st["mode"], st["provider"], st["available"]), ("gpt-live", "gemini", True))
        self.assertEqual(st["model"], "gemini-3.8-live")
        self.assertEqual(len(st["models"]), 4)
        self.assertNotIn("k", [v for v in st.values() if isinstance(v, str) and len(v) == 1])

    def test_gemini_status_unavailable_without_key(self):
        with mock.patch.object(vl, "_voice_section", return_value=_voice()), \
                mock.patch.object(vl, "_resolve_gemini_key", return_value=""):
            st = vl.resolve_gpt_live_status()
        self.assertFalse(st["available"])
        self.assertIn("Gemini", st["reason"])

    def test_openai_stays_default(self):
        voice = {"voice_chat_mode": "gpt-live", "gpt_live": {"model": "gpt-live-1"}}
        with mock.patch.object(vl, "_voice_section", return_value=voice), \
                mock.patch.object(vl, "_resolve_credentials", return_value=("", vl.DEFAULT_LIVE_BASE_URL)):
            st = vl.resolve_gpt_live_status()
        self.assertEqual(st["provider"], "openai")
        self.assertFalse(st["available"])
        self.assertEqual(vl.live_provider({"provider": "Google"}), "gemini")
        self.assertEqual(vl.live_provider({}), "openai")


if __name__ == "__main__":
    unittest.main()
