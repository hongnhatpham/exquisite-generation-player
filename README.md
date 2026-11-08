# Exquisite Generation: play with your ChatGPT plan

[Exquisite Generation](https://exquisite.bynhat.com) is an artwork: people and AI models draw one endless painting
together, one square tile at a time, and each player sees only a fifth of the tiles around theirs. This helper lets
your own ChatGPT model join in as a player, on your ChatGPT plan (Plus or Pro). You sign in with ChatGPT; nobody pays
for an API key.

## Play

You need Python 3.11 or newer.

```sh
git clone https://github.com/hongnhatpham/exquisite-generation-player
cd exquisite-generation-player
pip install pillow
python chatgpt.py login               # once: sign in with ChatGPT in your browser
python chatgpt.py play 3 --name YOU   # your model draws 3 tiles, signed "YOU's <model>"
```

While the game is in testing, also set the tester key you were given: `export EG_TESTER_KEY=...` (on Windows,
`set EG_TESTER_KEY=...`).

More: `python chatgpt.py models` lists the models your plan offers (pick one with `--model`), `--effort low|medium|high`
sets how hard it thinks, `login --paste` signs in with the browser on another computer, and `logout` signs out.

## What goes where

- Your sign-in stays on your computer, in `~/.config/exquisite-generation`. The game never sees it.
- Your model sees the edges of the tiles around its own, drawn by other players, so OpenAI sees them too. By playing,
  you agree to that.
- The game gets your model's drawing, its one-line note about what it thought was next to it, your nickname and the
  model's name. They are kept as part of the artwork and its research, without anything that identifies you.
- Each tile counts toward your ChatGPT plan's usage: see https://chatgpt.com/settings/usage. You can disconnect the
  app there at any time.

Sign in with ChatGPT for open-source tools is new, and OpenAI does not offer it to every account yet. If `play` stops
with `subscription_sharing_user_not_eligible`, your account can't use it for now.

## How it works

`harness.py` is the game turn: the tile is an SVG file your model edits with coding-agent tools (read, edit, write,
look at the picture, done), with the neighbours' edges in the file, read-only. `chatgpt.py` signs in with ChatGPT
(OpenAI's open-source flow) and sends the model's requests to your plan.
