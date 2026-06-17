# AI Agent Handoff: AWR1843 → ThingsBoard Range-Time

## Goal
Keep this project focused on the simplest working path:

1. Read raw `.bin` frames from AWR1843 / DCA1000.
2. Compute Range FFT in Python.
3. Publish each frame as telemetry to ThingsBoard.
4. Let ThingsBoard dashboard visualize a continuously updating range-time view.
5. Avoid storing the high-rate stream in the database when possible.

## Current implementation assumptions
- Python pushes telemetry over MQTT.
- ThingsBoard telemetry topic uses the short format `v2/t`.
- Dashboard widgets receive realtime updates over ThingsBoard WebSocket-based dashboard subscriptions.
- For custom heatmap-like rendering, use a custom widget and an external chart library such as ECharts.

## Files
- `awr1843_tb_publisher.py`: Python MQTT publisher.
- `awr1843_tb_range_time_guide.md`: full setup guide for VS Code + ThingsBoard.

## What may need adjustment
- Raw bin parser:
  - frame length
  - IQ packing order
  - endian
  - DCA1000 packet reassembly if needed
- FFT settings:
  - `fft_size`
  - window type
  - scaling / normalization
- Update rate:
  - frame pacing / replay speed
- Payload shape:
  - one array per frame (`range_profile`)
  - or flattened heatmap window if the widget prefers it

## Safe next steps for an agent
1. Replace the placeholder parser with a parser that matches the exact AWR1843 `.bin` format used in the project.
2. Add a `requirements.txt` if missing.
3. Add a minimal `README.md` with run commands.
4. Add a ThingsBoard custom widget JSON export if a heatmap widget is needed.
5. Add a small test file that verifies the FFT length and telemetry payload schema.

## Important ThingsBoard implementation notes
- Use device access token for MQTT authentication.
- Keep dashboard data on a realtime-only path when the deployment needs zero retention for this stream.
- A normal time-series chart can validate telemetry first; a custom widget can be added later for the heatmap effect.

## UI behavior target
The display should behave like a live waterfall / video stream:
- new frame arrives
- append to the top or bottom of the heatmap
- keep a rolling window of recent frames
- redraw immediately

## Do not change unless required
- Keep the MQTT publisher simple.
- Do not introduce a webserver unless the project explicitly needs one.
- Do not add database writes in the Python script.
