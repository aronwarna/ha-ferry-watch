# Ferry Watch for Home Assistant

A Home Assistant app (add-on) that follows the ferries using the Porkkala inner
route live on a map, with each ship's track since it left port.

## Install

1. In Home Assistant go to **Settings → Apps → Install app**.
2. Open the menu (top right) → **Repositories**, add
   `https://github.com/aronwarna/ha-ferry-watch` and select **Add**.
3. Find **Ferry Watch** in the list, install it, and start it.
4. Turn on **Show in sidebar**, then open **Ferry Watch** from the sidebar.

Details and options are in [ferry_watch/DOCS.md](ferry_watch/DOCS.md).

## Development

Run the panel locally with made-up ship movements (no internet needed):

```sh
cd ferry_watch/app
pip install -r requirements.txt
FW_DEV=1 FW_SIMULATE=1 FW_DATA_DIR=/tmp/fw python -m ferrywatch
# open http://localhost:8099
```

Leave out `FW_SIMULATE=1` to use the live Digitraffic feed. Tests:

```sh
pip install pytest
python -m pytest tests
```

Ship positions come from [Digitraffic](https://www.digitraffic.fi/en/marine-traffic/)
(Fintraffic, CC BY 4.0).
