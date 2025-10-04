"""
Speech-to-Text Module using Soniox Real-time STT
"""
import os
import sys
import json
import threading
import time

# Suppress ALSA/JACK warnings before importing pyaudio
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

# Redirect stderr to suppress ALSA warnings
stderr = sys.stderr
sys.stderr = open(os.devnull, 'w')
import pyaudio
sys.stderr = stderr

from websockets.sync.client import connect
from websockets import ConnectionClosedOK


# WebSocket URL
SONIOX_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

# Audio recording parameters
CHUNK = 1920  # 120ms at 16kHz
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000


class SonioxSTT:
    """Real-time Speech-to-Text using Soniox"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.ws = None
        self.audio_stream = None
        self.audio_thread = None
        self.stop_event = threading.Event()
        self.on_transcript_callback = None
        self.is_speaking = False  # Flag to suppress STT during TTS playback

    def set_speaking_mode(self, is_speaking: bool):
        """Set whether the bot is currently speaking (to suppress STT)"""
        self.is_speaking = is_speaking

    def get_config(self) -> dict:
        """Get Soniox STT config"""
        return {
            "api_key": self.api_key,
            "model": "stt-rt-preview",
            "language_hints": ["en"],
            "enable_endpoint_detection": True,
            "audio_format": "pcm_s16le",
            "sample_rate": RATE,
            "num_channels": CHANNELS,
        }

    def stream_audio_from_mic(self):
        """Stream audio from microphone to websocket"""
        p = pyaudio.PyAudio()

        try:
            stream = p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            print("🎤 Listening...")

            # Silence buffer to send during TTS playback
            silence = b'\x00' * CHUNK * 2  # 2 bytes per sample for 16-bit audio

            while not self.stop_event.is_set():
                try:
                    data = stream.read(CHUNK, exception_on_overflow=False)

                    if self.ws:
                        try:
                            # Send silence if bot is speaking, otherwise send mic audio
                            audio_to_send = silence if self.is_speaking else data
                            self.ws.send(audio_to_send)
                        except Exception as e:
                            if not self.stop_event.is_set():
                                print(f"\n⚠️ WebSocket send error: {e}")
                            break
                    time.sleep(0.001)
                except Exception as e:
                    if not self.stop_event.is_set():
                        print(f"Error reading audio: {e}")
                    break

            # Send empty string to signal end-of-audio (only if connection is still alive)
            if self.ws:
                try:
                    self.ws.send("")
                except:
                    pass  # Ignore errors when closing

        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    def extract_transcript(self, final_tokens: list) -> str:
        """Extract clean transcript from tokens"""
        if not final_tokens:
            return ""

        text_parts = []
        for token in final_tokens:
            text = token.get("text", "")
            if text:
                text_parts.append(text)

        # Join all text and clean up
        transcript = "".join(text_parts)
        # Remove <end> markers and other artifacts
        transcript = transcript.replace("<end>", "").strip()

        return transcript

    def start_listening(self, callback=None):
        """Start listening for speech with auto-reconnection"""
        self.on_transcript_callback = callback
        self.stop_event.clear()
        
        max_retries = 3
        retry_count = 0
        
        while not self.stop_event.is_set() and retry_count < max_retries:
            try:
                print("Connecting to Soniox...")
                
                with connect(
                    SONIOX_WEBSOCKET_URL, 
                    additional_headers={"Authorization": f"Bearer {self.api_key}"}
                ) as ws:
                    self.ws = ws
                    
                    # Send configuration
                    config = self.get_config()
                    ws.send(json.dumps(config))
                    
                    print("✓ STT Ready!")
                    
                    # Reset retry count on successful connection
                    retry_count = 0
                    
                    # Start audio streaming thread
                    self.audio_thread = threading.Thread(target=self.stream_audio_from_mic, daemon=True)
                    self.audio_thread.start()
                    
                    # Process messages
                    self._process_messages(ws)
                    
            except Exception as e:
                retry_count += 1
                if retry_count < max_retries:
                    print(f"⚠️ Connection failed (attempt {retry_count}/{max_retries}): {e}")
                    print("🔄 Retrying in 2 seconds...")
                    time.sleep(2)
                else:
                    print(f"❌ Max retries reached. STT failed: {e}")
                    break
        
        # Final cleanup
        self.stop()

    def _process_messages(self, ws):
        """Process incoming WebSocket messages"""
        final_tokens = []
        non_final_tokens = []
        last_token_time = time.time()
        processing = False

        try:
            while not self.stop_event.is_set():
                try:
                    message = ws.recv(timeout=0.1)
                except TimeoutError:
                    # Check for silence timeout
                    if final_tokens and (time.time() - last_token_time) > 3.0:
                        print()  # New line
                        transcript = self.extract_transcript(final_tokens)
                        if transcript and len(transcript.strip()) > 1:
                            if self.on_transcript_callback:
                                self.on_transcript_callback(transcript)
                        final_tokens = []
                        non_final_tokens = []
                        last_token_time = time.time()
                    continue

                res = json.loads(message)

                # Handle errors
                if res.get("error_code") is not None:
                    print(f"❌ STT Error: {res['error_code']} - {res['error_message']}")
                    break

                # Parse tokens
                non_final_tokens = []
                for token in res.get("tokens", []):
                    if token.get("text"):
                        if token.get("is_final"):
                            final_tokens.append(token)
                            last_token_time = time.time()
                        else:
                            non_final_tokens.append(token)

                # Display current transcription progress
                if final_tokens or non_final_tokens:
                    current_text = self.extract_transcript(final_tokens)
                    current_non_final = "".join([token.get("text", "") for token in non_final_tokens])
                    current_non_final = current_non_final.replace("<end>", "")

                    if current_non_final:
                        print(f"\r🎤 [{current_text} {current_non_final}...]", end="", flush=True)
                    elif current_text:
                        print(f"\r🎤 {current_text}", end="", flush=True)

                # Check for endpoint detection OR silence timeout
                endpoint_detected = res.get("endpoint_detected", False)
                silence_timeout = final_tokens and (time.time() - last_token_time) > 2.5

                if (endpoint_detected or silence_timeout) and final_tokens and not processing:
                    processing = True
                    print()  # New line

                    # Extract transcript
                    transcript = self.extract_transcript(final_tokens)

                    if transcript and len(transcript.strip()) > 1:
                        # Call callback with transcript
                        if self.on_transcript_callback:
                            self.on_transcript_callback(transcript)

                    # Reset for next input
                    final_tokens = []
                    non_final_tokens = []
                    last_token_time = time.time()
                    processing = False

                # Session finished
                if res.get("finished"):
                    print("\n✓ STT session finished.")
                    break

        except ConnectionClosedOK:
            print("\n✓ STT connection closed normally.")
            raise
        except KeyboardInterrupt:
            print("\n✓ STT stopped by user.")
            self.stop_event.set()
            raise
        except Exception as e:
            if "408" in str(e) or "timeout" in str(e).lower():
                print(f"\n⚠️ STT timeout: {e}")
                raise
            else:
                print(f"\n❌ STT Message Error: {e}")
                raise

    def stop(self):
        """Stop listening"""
        self.stop_event.set()
        if self.audio_thread:
            self.audio_thread.join(timeout=2)
        self.ws = None
