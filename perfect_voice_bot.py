#!/usr/bin/env python
"""
PERFECT VOICE BOT: STT → LLM → Streaming TTS
Complete voice assistant with zero-latency streaming
"""
import os
import queue
import threading
import subprocess
from groq import Groq
from google.cloud import texttospeech
from dotenv import load_dotenv
from stt import SonioxSTT
from prompts import HOTEL_SERVER_SYSTEM_PROMPT

load_dotenv()


class PerfectVoiceBot:
    """Perfect voice bot with streaming STT → LLM → TTS"""

    def __init__(self, voice_name: str = "en-US-Chirp3-HD-Charon", language_code: str = "en-US", min_chunk_words: int = 3):
        # Initialize Groq LLM
        self.groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.llm_model = "meta-llama/llama-4-scout-17b-16e-instruct"

        # Initialize Google Cloud TTS
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        self.tts_client = texttospeech.TextToSpeechClient()

        # Initialize Soniox STT
        self.stt = SonioxSTT(api_key=os.getenv("SONIOX_API_KEY"))

        # TTS config
        self.voice_name = voice_name
        self.language_code = language_code
        self.min_chunk_words = min_chunk_words

        # Queue for text chunks
        self.text_queue = queue.Queue()

        # Control
        self.running = False
        self.tts_active = False

        # Conversation history
        self.conversation_history = []

        print("✓ PERFECT Voice Bot initialized")
        print(f"  STT: Soniox Real-time")
        print(f"  LLM: {self.llm_model}")
        print(f"  TTS: Google Cloud {voice_name}")
        print(f"  Min chunk: {min_chunk_words} words")

    def _tts_streaming_worker(self):
        """Worker: Stream text to TTS and play audio continuously"""
        # Notify STT that speaking has started
        self.stt.set_speaking_mode(True)
        self.tts_active = True

        # Start aplay process for continuous audio (PCM 16-bit LE, 24kHz, mono)
        aplay_process = subprocess.Popen(
            ['aplay', '-q', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-t', 'raw'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        chunk_count = 0
        text_chunk_count = 0

        try:
            # Configure streaming TTS
            streaming_config = texttospeech.StreamingSynthesizeConfig(
                voice=texttospeech.VoiceSelectionParams(
                    name=self.voice_name,
                    language_code=self.language_code,
                )
            )

            # Request generator for streaming TTS
            def request_generator():
                # First request: config
                yield texttospeech.StreamingSynthesizeRequest(
                    streaming_config=streaming_config
                )

                # Subsequent requests: text chunks
                while self.running:
                    try:
                        text = self.text_queue.get(timeout=0.1)

                        if text is None:  # Stop signal
                            break

                        nonlocal text_chunk_count
                        text_chunk_count += 1

                        yield texttospeech.StreamingSynthesizeRequest(
                            input=texttospeech.StreamingSynthesisInput(text=text)
                        )

                        self.text_queue.task_done()

                    except queue.Empty:
                        continue

            # Stream TTS responses
            streaming_responses = self.tts_client.streaming_synthesize(request_generator())

            for response in streaming_responses:
                if response.audio_content:
                    chunk_count += 1

                    # Write directly to aplay stdin (continuous stream)
                    aplay_process.stdin.write(response.audio_content)
                    aplay_process.stdin.flush()

        except Exception as e:
            print(f"\n⚠️  TTS streaming error: {e}")

        finally:
            # Close aplay gracefully
            if aplay_process.stdin:
                aplay_process.stdin.close()
            aplay_process.wait(timeout=5)

            # Notify STT that speaking has ended
            self.stt.set_speaking_mode(False)
            self.tts_active = False

    def _should_send_chunk(self, buffer: str) -> tuple[bool, str]:
        """
        AGGRESSIVE chunking for low latency
        Returns: (should_send, text_to_send)
        """
        buffer = buffer.strip()

        if not buffer:
            return False, ""

        words = buffer.split()

        # Complete sentence (highest priority)
        if buffer[-1] in '.!?':
            return True, buffer

        # Comma with enough words (natural pause point)
        if ',' in buffer:
            parts = buffer.rsplit(',', 1)
            if len(parts[0].split()) >= self.min_chunk_words:
                return True, parts[0] + ','

        # Long phrase (send partial to reduce latency)
        if len(words) >= self.min_chunk_words * 2:
            # Send all but last word (keep partial word in buffer)
            return True, ' '.join(words[:-1])

        return False, ""

    def _process_llm_response(self, prompt: str):
        """Process LLM response with streaming TTS"""
        print(f"\n🤖 Assistant: ", end="", flush=True)

        # Start TTS worker
        self.running = True
        tts_thread = threading.Thread(target=self._tts_streaming_worker, daemon=True)
        tts_thread.start()

        # Build conversation context
        messages = [
            {
                "role": "system",
                "content": HOTEL_SERVER_SYSTEM_PROMPT
            }
        ]

        # Add conversation history (last 5 exchanges)
        for msg in self.conversation_history[-10:]:
            messages.append(msg)

        # Add current prompt
        messages.append({"role": "user", "content": prompt})

        # Stream from LLM
        stream = self.groq_client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            temperature=0.7,
            max_tokens=512,
            stream=True
        )

        text_buffer = ""
        full_response = ""

        # Process LLM tokens with aggressive chunking
        for chunk in stream:
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                text_buffer += token

                print(token, end="", flush=True)

                # Check if we should send chunk to TTS
                should_send, text_to_send = self._should_send_chunk(text_buffer)
                if should_send:
                    self.text_queue.put(text_to_send)
                    # Keep remaining text in buffer
                    text_buffer = text_buffer[len(text_to_send):].lstrip()

        # Send remaining text
        if text_buffer.strip():
            self.text_queue.put(text_buffer.strip())

        print("\n")

        # Signal completion to TTS worker
        self.text_queue.put(None)

        # Wait for TTS to finish
        tts_thread.join(timeout=60)

        self.running = False

        # Update conversation history
        self.conversation_history.append({"role": "user", "content": prompt})
        self.conversation_history.append({"role": "assistant", "content": full_response})

        return full_response

    def _on_transcript(self, transcript: str):
        """Callback when STT detects speech"""
        if self.tts_active:
            # Ignore if bot is speaking
            return

        print(f"\n💬 You: {transcript}")

        # Process with LLM and respond
        self._process_llm_response(transcript)

        # Ready for next input
        print("\n🎤 Listening...")

    def start(self):
        """Start the voice bot"""
        print("\n" + "=" * 70)
        print("🤖 PERFECT VOICE BOT - Ready!")
        print("Speak naturally. Say 'goodbye' or 'exit' to quit.")
        print("=" * 70 + "\n")

        try:
            # Start listening with callback
            self.stt.start_listening(callback=self._on_transcript)

        except KeyboardInterrupt:
            print("\n\n✓ Voice bot stopped by user")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stt.stop()
            print("\n✓ Goodbye!")


def main():
    """Run the perfect voice bot"""
    # Initialize voice bot
    bot = PerfectVoiceBot(
        voice_name="en-US-Chirp3-HD-Charon",  # High-quality voice
        language_code="en-US",
        min_chunk_words=3  # Aggressive chunking for low latency
    )

    # Start listening and responding
    bot.start()


if __name__ == "__main__":
    main()
