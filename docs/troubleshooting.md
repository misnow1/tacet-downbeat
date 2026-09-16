# When it goes wrong

The box fails visible. Anything it cannot confirm, it says. This is every
message it, the page and the mirror script can show, with what each needs from
you. The runbook is [gameday.md](gameday.md).

**First, always:** the operator keeps the fader. Whatever below has happened,
ride the band from StageMix and keep going. The annotation log is the source of
truth and the recording runs in Reaper, so neither is lost when the page or the
markers misbehave.

---

## The page

| The page says | What it means | What to do |
|---|---|---|
| The counter has stopped changing | The page itself has stopped, not the box. This is what no banner cannot tell you on its own | Reload the page. The box, the log and the recording are unaffected |
| `Not connected to the box` | The websocket is down. The page is retrying once a second | Everything on screen is the last thing the box said. The console is unaffected; ride the fader from the DM7 app if it does not come back |
| `Connecting to the box` | The socket opened but the box has not delivered anything through it yet | Normal for a moment. If it stays, the box is up and wedged rather than down |
| `No word from the box for Ns` | The socket still looks open and nothing is arriving through it, which is what stadium wifi does as the stands fill | Treat the whole page as stale. The link usually drops properly a moment later and the retry takes over |
| `Tap did not reach the box (...)` (red, under the state) | That tap's request failed or got no answer within 10 seconds: wifi dropped, or the box is gone. The box never heard it, so nothing else on the page will say so. It stays until a later tap gets through | Look at the fader on the console or StageMix. If the tap did not happen, do it there or tap again. A request that timed out can still arrive late |
| `Sent before the page had timed its link to the box` | The tap went before the page's first clock round trip with the box, so the log has when it arrived but not how late. Normal for a moment after the page connects | Nothing |
| `That tap was not done: it took Ns to reach the box` (white on red, under the state) | A fader tap - OPEN, FADE OUT, Stand down, or a fader button in the grid - reached the box more than `fader.stale_tap_seconds` (2 s by default) after it was tapped, even allowing for the clock estimate's error. Acting on it would have opened into silence or cut a phrase, so nothing moved. It is in the log with its true tap time; a grid button keeps its reason, marked `stale` and not executed | Look at the band and the fader, and tap again if you still mean it. If it keeps happening the wifi is struggling: ride the fader from StageMix. Annotations are never refused this way |
| A button with an amber ring | That tap is on its way and the box has not answered yet | Wait a moment before tapping again. If it lingers, the link is slow; the red line above says so if it fails |
| `LINK LOST` | Reaper stopped answering while its transport should have been streaming. It appears within about three seconds of the last packet (two of silence, then the box's once-a-second re-check), whether or not anything else is happening | Check Reaper is alive. The audio may still be recording. A recording line that still says `ROLLING` is Reaper still talking |
| `not yet reported` | Link is fine, Reaper has not said whether it is recording | Normal after a box restart. Any transport change in Reaper resolves it |
| `Console unreachable` | The DM7 did not accept a packet. If it happened partway through a fade or ride-in, the fader stopped wherever it had got to, and the why line adds *The last fader move did not finish; tap it again to retry* | The operator has the fader. Ride it from the iPad and keep going. Once the link is back, tapping the same button again finishes the move from where it stopped - FADE OUT fades down from there, it does not jump back up first |
| `Log not saving: ...` (red, under the link banner) | The last annotation or fader entry did not reach the disk: usually a full disk, sometimes a drive that went away. Fader moves are unaffected; the entries are not Entries are written just after the tap, not during it, so the banner can appear a moment after the tap that caused it. A span button that goes back to what it said before the tap is that span's start or end not saving | Free space on the log's disk (`df -h`). The next tap saves again and the banner clears on its own. Write down what happened meanwhile on paper, since nothing can recover those annotations. If the message says the file has gone away, the box will not recreate it: restart the box with a fresh `--log` |
| `Log not saving: the log writer has stopped; ...` | The thread that writes the log has ended. It should never happen, and nothing more will be saved: every tap from here is lost from the log. Fader moves are unaffected | Restart the box when there is a break in play (Ctrl-C twice, then start it again with the same `--log`). Paper notes until then. File an issue with the terminal output, which will carry the traceback |
| `N log entries were not saved earlier` | The log failed and has since recovered. The count is how many entries are missing | Nothing now. Note it for the review; a gap in the log's `seq` numbers marks where each one was |
| `Reaper markers not updating: ...` (amber) | The mirror queue could not be written. The log is still saving, so nothing is lost; live markers in Reaper just stop appearing | Free space if that is the cause, and the markers resume. Missing ones can be rebuilt from the log afterwards |
| `Could not send the start to Reaper` | The box could not send `/record` at all, so nothing started | Tap again, or start the recording in Reaper. Check `--reaper-host` if it keeps happening |
| `Reaper is already recording` | Second press of the record button | Nothing. It refused on purpose |
| `Reaper has not confirmed the start sent Ns ago` | The record button was pressed and Reaper has not yet said it is recording. Over bad wifi a double tap arrives as two presses before Reaper answers, and `/record` is a toggle, so a second one would stop the take. The box holds the button until Reaper answers, and never gives up waiting on its own | Look at Reaper. If it is recording, nothing - the page catches up the moment Reaper says so. If it is not, start it in Reaper; the box picks that up too |
| `data 'text' is N characters; the most is 1000` | A note longer than the log takes. Nothing was recorded, and nothing moved | Tap Note again and say it shorter |
| `Armed by that open: the box was standing down` | You opened the fader while the box was standing down, so it armed itself and opened (#89). The tap is never refused: a forgotten Arm must not cost a downbeat | Nothing. If the band is not really in the stands, tap **Stand down** when the fader is back down |
| **Start recording** greyed out, Reaper stopped | Expected on this rig. Reaper is never silent, so the box cannot infer the transport is parked and will not send a toggle blind | Roll a recording in Reaper and stop it. See [the greyed-out record button](reaper.md#the-record-button-is-greyed-out-before-you-touch-anything) |

The top banner is about the iPad's link to the box. `LINK LOST` on the
recording line is about the box's link to Reaper. They are different failures
and can happen separately: the box can be talking to Reaper perfectly while the
iPad cannot see the box.

---

## The terminal

The box can also refuse to start at all, or start with a warning. Those are
config and log problems and it says so on the terminal rather than the page:

| The terminal says | What it means | What to do |
|---|---|---|
| `config      none (flags only)` in the banner | It found no `tacet.toml`. Not an error, but if you expected one you are in the wrong directory | `cd` to the repo, or pass `--config <path>` |
| `WARNING     this log already contains a recording` | The `--log` you gave already holds a `recording-started` -- yesterday's game, or the pre-flight test. Markers derived from it anchor to **that** recording. Entries stamped with Reaper's playhead still land correctly; any made while Reaper was parked fall back to arithmetic from the wrong anchor | Stop and pass a fresh `--log`, unless you genuinely meant to append. Nothing is lost either way; the log is append-only and can be split afterwards |
| `WARNING     log ends in a torn write` (or `queue ends in a torn write`) | The last run stopped in the middle of writing a line: a crash, a kill, a laptop that lost power, or a full disk. Appending straight after that fragment used to corrupt the log so the *next* restart refused to start | Nothing. It is repaired as the box opens the file: a whole entry that only lost its newline is kept, and anything else is moved to the `.torn` file named on the next line, never deleted. Everything before it is intact. If the disk is full, that is the thing to fix |
| `WARNING     the clock has restarted since this log was written` | The `--log` you gave was written before this machine last rebooted. Offsets in it are measured on a clock that has since started again from zero | Pass a fresh `--log`. If you must append, entries stamped with Reaper's playhead still land correctly; anything placed by arithmetic after the reboot will not |
| `WARNING     this log has N span(s) still open from an earlier run` | The `--log` you gave has spans that were started and never ended -- last game's `Q4`, say -- listed underneath with when each started. The box resumes them, so their buttons read "(end)" | If this is the box restarting mid-game, nothing: those are today's spans and resuming them is right. If it is a new game, stop and pass a fresh `--log` |
| `--log ... cannot be read: line N: ...` | The log has a line the box cannot read. `schema vN, which this build cannot read` means a different version of tacet wrote it, usually a newer one read after a rollback; anything else is a damaged line. The box refuses before it starts, and has changed nothing in the file | Pass a fresh `--log` and start. Keep the unreadable one for afterwards; do not edit it on the day |
| `unknown key '...' in [...]` | A misspelled key. It refuses rather than silently using a default and driving the wrong fader | Fix the spelling; the message lists the keys that exist |
| `... must be a whole number` / `must be true or false` | A value of the wrong type, e.g. `dca = "3"` with quotes | Drop the quotes. Numbers and booleans are bare in TOML |
| `... must be a port from 1 to 65535, got ...` | A port that cannot exist, in the config file (the key is named) or on the command line (`argument --console-port: ...`). Usually a digit too many: `499000` for `49900`. It refuses because a send to such a port fails in a way that used to leave the page showing a healthy fader | Fix the number. The defaults are console `49900`, Reaper send `8000` and feedback `9000`, page `8080` |
| `--config names ..., which does not exist` | A config was asked for by name and is not there | Check the path. A file merely looked for and absent is fine; one you named is not |
| `--console-host is required or console.host in the config file` | Neither the flag nor the file supplied it | Give it either way |
| `--log is required on the command line, a fresh one each game` | No `--log`. It is never read from the config file, so a complete `tacet.toml` does not supply it | Add `--log ~/games/<YYYY-MM-DD>.jsonl` with today's date |
| `capture.log is no longer a config key` | A `tacet.toml` written before the log became command-line only | Delete the `log = ...` line from `[capture]` and pass `--log` instead |
| `capture.audio_path is not set, so there is no telling whether the recording fits` | The box does not know where Reaper records, so it cannot check there is room for the game. It refuses rather than start unchecked (#53) | Set `audio_path` and `channels` under `[capture]` in `tacet.toml`. For a test run that records nothing, add `--no-disk-check` |
| `capture.channels is required with capture.audio_path` | The recording path is set, but not how many tracks this game records, which is what the size is worked out from | Set `channels` to the number of armed tracks on [the patch list](reaper.md#tracks-to-record) |
| `capture.audio_path ... does not exist or is not a directory - is the NAS mounted?` | The recording path is not there. Usually the NAS share is not mounted, and measuring whatever is underneath would report the local disk as the NAS | Mount the share (Finder > Go > Connect to Server), confirm the path in Finder, and start again. `--no-disk-check` only if you are deliberately recording somewhere else |
| `only ... free on ..., and a full game needs ~...` | Not enough room on the recording volume for the channels and hours in the message. Running out mid-game loses the recording | Free space on that volume, or record somewhere with room. If the game is short or the count is wrong, fix `channels` / `game_hours`. `--no-disk-check` starts anyway, and the banner says it did |
| `only ... free on ..., where the log or queue is written` | The disk holding `--log` or `--queue` is nearly full. The files are small, but a full disk tears them | Free space there (`df -H`) and start again |
| `capture.channels must be at least 1, not ...` / `capture.game_hours must be more than 0, not ...` | A count or a length that cannot be right | Fix the value |
| `disk        not checked (--no-disk-check)` in the banner | The box was started with the override and had no recording path to measure | Nothing, if that was deliberate. Otherwise stop, set `capture.audio_path`, and start without the flag |

---

## The mirror console

**The mirror script talks in Reaper's console, not on the page.** Everything it
can say, and whether it needs you:

| The console says | What it means | What to do |
|---|---|---|
| `mirroring <path> from byte N` | Normal. `N` is where it left off last game | Nothing |
| `queue does not exist yet; waiting for the box to create it` | Normal before the box is started | Start the box |
| `no queue path given; not starting` | The path prompt was cancelled or left empty. **The script is not running** and nothing will mirror | Run it again from the action list and enter the path. It is not remembered until it is entered once |
| `mirror stopped` | The script has exited - the action was run a second time, or Reaper closed | Markers stop, the log does not. Re-run it if the game is still going |
| `no remembered position in a queue with history; starting at its end` | Reaper forgot the position - a reinstall, a cleared `reaper-extstate.ini`, a different machine. It refused to read the queue from the beginning, which on a new project would have stamped every marker of every past game onto today's timeline | Nothing. This session mirrors normally from here. If the box was already running, the few events written before the script came up were skipped - they are in the log |
| `queue is shorter than the stored offset; starting from the beginning` | The file was cleared or replaced, so it is reading all of it | Expect markers for whatever is in that file. If it holds an earlier game, those markers are wrong: delete them in Reaper. The log is unaffected |
| `ignoring malformed queue line (<why>): <line>` | A line that is not one the box writes: two lines glued together after a crash, a fragment, or a box and a script from different versions. That line gets no marker; the lines after it still do | Nothing mid-game; the entry is in the log. If it repeats on every line, the box and the script disagree about the format - update both from the same checkout |
| `no start for span <id>; placing a marker instead` | A span ended that began before the script was watching, so there is no region to draw. It leaves a marker labelled `(end)` instead | Nothing. The whole region can be rebuilt from the log |

The *shorter than the stored offset* row is the reason for *do not rotate the queue* in [reaper.md](reaper.md#the-mirror-script). None of
these costs annotation data: the log is written by the box and does not depend
on the script at all.

---

## Other things

**A restarted box mid-game cannot start recording**, and says so. It has heard
`/time` but no transport change, so it cannot tell whether Reaper is rolling,
and will not risk stopping a live recording to find out. Start or stop in Reaper
directly; the box will pick the state up from that change.

**If the mirror script dies**, the markers stop and the data does not. The
annotation log is the source of truth and every marker is derivable from it
afterwards. Do not stop the game over it, and do not try to fix it mid-game.

Note that `tacet.markers` is a library and there is no command that
regenerates a project from a log yet. Recovery is possible but is not a
button - it is not something to attempt on the day.
