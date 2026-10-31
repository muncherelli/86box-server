# Box display size

Each box has its own virtual screen (a TigerVNC `Xvnc` display). A small window manager (`openbox`, one per box, `wm@<name>.service`) makes the 86Box window fill that screen, with 86Box's menu bar still visible. 86Box scales whatever the guest shows to the window, so text, CGA, EGA, VGA and SVGA modes all fill the screen and switching modes does not change the window.

## Choosing a size

On the box's edit page, **Display size**:

- **auto** (default): the screen follows your browser window, live. Resize the tab and the guest screen resizes with it. Nothing to restart.
- **A fixed size**: pick a preset or type `WxH` (640x400 up to 2560x1600). The console scales it to fit your window. It applies the next time the box starts.

Presets: 640x400, 640x480, 720x400, 800x600, 1024x768, 1152x864, 1280x1024, 1600x1200, 1280x720, 1366x768, 1440x900, 1600x900, 1680x1050, 1920x1080, 1920x1200, 2560x1440.

A display smaller than the guest's mode still works: the picture is scaled down. A larger display just scales it up. For the sharpest picture, use a display at least as big as the guest's biggest mode. The guest picture is stretched to the whole window, so a widescreen display stretches a 4:3 guest.

## How it fits together

- The size is stored as `resolution` in `vmdefs/<name>.json` and as `B86_GEOMETRY` in `vmdefs/<name>.env`; `vnc@.service` passes it to `Xvnc -geometry`. Old env files without it use 1024x768.
- The console link uses noVNC's `resize=remote` for `auto` (the browser asks Xvnc to change size) and `resize=scale` for a fixed size.
- `/opt/86box-server/openbox-rc.xml` tells openbox to maximize the main 86Box window and drop its title bar. The 86Box Settings dialog stays a normal window.
- 86Box must have a resizable window for this. New boxes get `vid_resize = 1` from the template; for older boxes, `libexec/ensure-resize` adds the key at start if it is missing. If you turn "Resizeable window" off in 86Box's Settings, the window stays at its own size.
- Existing boxes pick all of this up the next time they are started (the display itself is only created at start).
