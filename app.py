import logging
import signal
import subprocess
import threading
import time
from pathlib import Path
from queue import Queue
from subprocess import Popen

import RPi.GPIO as GPIO
from statemachine import State, StateMachine

EARPIECE_PIN = 16
PULSE_PIN = 12
PULSE_SECONDS = 0.05


class BaseAudio:
    def __init__(self):
        self.current_process: Popen = None

    def _execute(self, command: list[str]):
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        self.current_recording_process = process

    def stop(self):
        if not self.current_process:
            return
        self.current_process.terminate()
        self.current_process.wait()
        self.current_process = None

    def wait(self):
        self.current_process.wait()


class AudioRecorder(BaseAudio):
    def __init__(self):
        super().__init__()

    def record(self, number: str):
        folder_path = Path(number)
        folder_path.mkdir(parents=True, exist_ok=True)
        output_file = self.get_unique_filename(folder_path)
        self._execute(["rec", "-t", "mp3", output_file])
        return self

    @staticmethod
    def get_unique_filename(folder_path: Path) -> str:
        index = 1
        while True:
            filename = folder_path / f"{index}.mp3"
            if not filename.exists():
                return filename
            index += 1


class AudioPlayer(BaseAudio):
    def __init__(self):
        super().__init__()

    def play(self, file) -> Popen:
        if not Path(file).exists():
            logging.error("Audioplayer: File does not exist.")
            return
        self.stop()
        self._execute(["play", file])
        return self

    def dial(self):
        self.play("dial.mp3")

    def beep(self):
        self.play("beep.mp3")


# sudo apt-get install sox libsox-fmt-all
class Phone(StateMachine):
    idle = State(name="Idle", initial=True)
    dialing = State(name="Dialing")
    answering = State(name="Answering")

    pickup = idle.to(dialing)
    dial = dialing.to(answering)
    answer = answering.to(idle)
    hang_up = idle.to(idle) | dialing.to(idle) | answering.to(idle)

    def __init__(self):
        self.e = threading.Event()
        self.earpiece_queue: Queue[bool] = Queue()
        self.audio_player = AudioPlayer()
        self.audio_recorder = AudioRecorder()
        self.__setup_gpio()
        super(Phone, self).__init__()

    def start(self):
        dial_thread = threading.Thread(
            target=self._get_dial,
            daemon=True,
        )
        earpiece_thread = threading.Thread(
            target=self._get_earpiece,
            daemon=True,
        )
        earpiece_thread.start()
        dial_thread.start()
        earpiece_thread.join()
        dial_thread.join()

    def stop(self):
        self.e.set()
        self.audio_player.stop()
        self.audio_recorder.stop()

    def __setup_gpio(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(EARPIECE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(
            EARPIECE_PIN,
            GPIO.BOTH,
            self.earpiece_queue.put,
            bouncetime=200,
        )

    def _get_earpiece(self):
        while not self.e.is_set():
            if self.earpiece_queue.empty():
                continue
            _ = self.earpiece_queue.get(block=False)
            earpiece_home = bool(GPIO.input(EARPIECE_PIN))
            if not earpiece_home:
                # Further debounce of switch
                time.sleep(0.75)
                if not self.current_state == Phone.idle:
                    continue
                self.pickup()
            else:
                self.hang_up()

    def _get_dial(self):
        num, prnt, last = 0, False, False
        dialed_numbers = []
        last_dial_time = time.time()
        while not self.e.is_set():
            if self.current_state != Phone.dialing:
                continue
            pulse = bool(GPIO.input(PULSE_PIN))

            if pulse and (pulse != last):
                last, prnt = True, True
                num += 1
                time.sleep(PULSE_SECONDS)
                continue

            if not pulse and (pulse != last):
                last = False
                time.sleep(PULSE_SECONDS)
                continue

            if not pulse and (pulse == last) and prnt:
                if num == 10:
                    num = 0
                self.audio_player.stop()
                logging.debug(f"get_dial: dialed {num}.")
                dialed_numbers.append(num)
                num, prnt, last = 0, False, False

                # Reset the timer for the last dial
                last_dial_time = time.time()

            # Check if there is a 5-second gap and some numbers since the last dial
            current_time = time.time()
            if current_time - last_dial_time > 1.5 and dialed_numbers:
                self.dial(number="".join(map(str, dialed_numbers)))
                dialed_numbers = []
                last_dial_time = current_time

        else:
            logging.warning("Pulser received signal.")

    def on_enter_idle(self):
        logging.info("Hung up wtf.")
        self.audio_recorder.stop()
        self.audio_player.stop()

    def on_exit_idle(self):
        logging.info("Phone picked up!")

    def on_enter_dialing(self):
        logging.info("Playing the wait tone meanwhile.")
        self.audio_player.dial()

    def on_exit_dialing(self):
        logging.info("Stopping the wait tone.")
        self.audio_player.stop()

    def on_enter_answering(self, number: str):
        logging.info(f"Dealed: {number}")
        logging.info("Answer after the peep.")
        self.audio_player.play("beep-long.mp3").wait()
        logging.info("Recording your answer now.")
        self.audio_recorder.record(number=number)


def replace_asoundrc_file(soundcard: str):
    content = """
        pcm.!default {
            type hw
            card ###
        }
        ctl.!default {
            type hw
            card ###
        }
    """.strip().replace(
        "###", soundcard
    )
    asoundrc_file_path = Path.home() / ".asoundrc"
    soundcard = get_usb_audio_card()
    with open(asoundrc_file_path, "w") as asoundrc_file:
        asoundrc_file.write(content)


def get_usb_audio_card():
    for line in (
        subprocess.check_output(["aplay", "-l"]).decode("utf-8").strip().splitlines()
    ):
        if "USB Audio Device" in line:
            return line[5]
    raise Exception("No USB Audio Interface detected.")


def main():
    logging.basicConfig(level=logging.DEBUG)
    replace_asoundrc_file(soundcard=get_usb_audio_card())
    p = Phone()
    signal.signal(
        signalnum=signal.SIGINT,
        handler=lambda s, f: p.stop(),
    )
    signal.signal(
        signalnum=signal.SIGTERM,
        handler=lambda s, f: p.stop(),
    )
    p.start()


if __name__ == "__main__":
    main()
