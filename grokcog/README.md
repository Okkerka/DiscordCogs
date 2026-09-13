# GrokCog

Groq-powered questions and web fact checks for Red. Requires Python 3.11+.
`grokcog` is the package name; the provider is **Groq**, not xAI Grok.

## Everyday usage

- `@bot whats 9x9?` — direct answer through the configured chat model.
- `@bot is this true?` while replying to a message — reads the quoted message's text
  and embeds, requests a web search, and replies to your question with evidence.
- `@bot is it true that ...?` — checks a claim written in the question itself.
- Attach an image with `@bot what does this say?`, or reply to a picture with
  `@bot is this true?`. Select a supported Qwen model first (see below).
- Reply to the bot's answer to ask a follow-up. Mentioning the bot also works when
  the original message isn't cached; it fetches the replied-to message from that channel.
- `>grok question` or `/grok ask question` — ordinary questions, with automatic
  search for common fact-check/current-information phrases.
- `>grok search question` or `/grok search question` — explicitly request search.
- `>grok cancel`, `>grok stats`, `>grok models` — also available under `/grok`.

Automatic routing recognizes common English fact-check/search phrases, including
"is this true", "fact check", "verify", "look this up", "latest", "current" and
"sources". Use `/grok search` for other phrasing or languages when search is needed.
Search uses `groq/compound` and enables only web search and website visits. Ordinary
questions use the configured chat model. No alternate provider is called silently.
Searching can incur Groq tool charges in addition to model usage.

Replies include up to 6,000 characters of quoted text, including embed descriptions
and fields. Longer context is marked as truncated. Missing/deleted/inaccessible
messages produce a clear error. Longer conversation chains, videos, GIFs and
document attachments are not read.
Enable Message Content Intent for mention/reply/prefix behavior, and give the bot
View Channel, Read Message History, Send Messages and Embed Links permissions.

Answers remain public in the invoking channel. Long answers have invoker-only
Previous/Next buttons that expire after three minutes. Search results are evidence,
not guaranteed truth. A search without usable source metadata does not display an
unsupported verification verdict. Self-reported confidence percentages and the old
unconditional "Fact-Checked" badge have been removed.

## Images and screenshots

Select `qwen/qwen3.8-27b` or `qwen/qwen3.6-27b` with `>grok admin setmodel` to use
image input. The bot reads PNG, JPEG and WebP attachments on your question and on
the message you replied to. Embedded images/thumbnails use Discord's image proxy;
author avatars/icons are not included. On `/grok ask` and `/grok search`, use the
optional `image` attachment field. Prefix commands automatically use attachments.
After upgrading, run `>slash sync` to expose the new image option.

Ordinary image questions go directly to Qwen. Image fact checks first ask Qwen to
read the relevant text, numbers and visual details, then pass those observations
and the original question to Compound for web evidence. Image observations are
explicitly treated as uncertain, untrusted transcription. Blurry text and cropped
tables can limit the answer; no model can guarantee an exact transcription.
If a text-only model is selected, the cog asks the owner to select Qwen; it does
not silently switch your configured model or ignore the image.

Up to three images are accepted per question, including replied-to images.
Known attachments are capped at 8 MB each and 16 MB total. Embedded image sizes
are checked by Groq's image service. The bot sends HTTPS Discord CDN/proxy URLs
to Groq, including the signed query string needed to access attachments; it does
not download images to disk or fetch arbitrary user URLs on the bot server.
Groq fetches the images. Inaccessible/expired URLs or provider image limits can
still fail. Images use the same API rate limits, timeouts and cancellation controls;
fact checks use two model calls, so image checks consume more free-tier capacity.
Each image currently counts as 2,048 input tokens on these Qwen models.
[Groq image input documentation](https://console.groq.com/docs/vision).

## Setup and existing installations

1. Reload/load `grokcog`.
2. Set a Groq key with `/grok admin apikey` (private acknowledgement), or DM the bot
   with `>grok admin apikey KEY`. Prefix key submission in a server is rejected; the
   cog attempts to delete the message. Rotate a key if it was posted publicly.
3. Run `>grok models`, then `>grok admin setmodel MODEL_ID` if necessary. Model
   selection verifies chat compatibility before saving. The catalog may include
   audio models that cannot answer chat questions. Run `models` first to populate
   the owner-only model autocomplete cache.
4. Run `>grok admin verify` to test the configured key/model. Separately run
   `>grok admin verify search` to test Compound source retrieval. On slash, select
   `mode: search`. A working chat model does not establish that search works.
5. Enable slash commands: `>slash enablecog GrokCog`, then `>slash sync`.

Replace `>` with your configured prefix. Admin commands also have slash equivalents.
Key/model/limits/cache commands remain bot-owner-only. `admin toggle` requires
server admin or Manage Server permissions and cannot run in DMs.

The existing Config namespace, saved key, settings and user statistics are preserved.
New installations default to `openai/gpt-oss-120b`. A previously saved Kimi model
is **not** silently replaced: if unavailable, select an available chat model above.
There is no automatic spending or provider fallback on model failure.

`admin cooldown SECONDS` (0–3600) applies to questions from prefix, slash, mentions,
replies and DMs. `admin ratelimits PER_MINUTE MIN_GAP` accepts 1–600 API attempts/min
and a 0–60 second gap. Every HTTP attempt, including retries/verification/model
discovery, uses the global limiter. The old internal `request_queue_enabled` setting
is retained for config compatibility; requests now always use bounded scheduling.
Up to three requests run concurrently, with at most 128 distinct pending questions,
a 30-second capacity wait and a 150-second overall provider-request deadline.
The configured HTTP timeout (default 120 seconds) is bounded to 5–120 seconds.

Cache keys preserve case and include channel, model, temperature, date and context.
Up to 256 answers are cached in memory, with a one-hour reuse TTL for direct answers
and five minutes for searches. Identical simultaneous requests share work. Cancelling
one user does not cancel other waiters; cancelling the last waiter stops the request.
Cached responses count in user statistics. `admin clearcache` also prevents older
in-flight requests from repopulating the cleared cache.

## Privacy and verification

Questions, quoted text and selected Discord image URLs are sent to Groq. The cog persists user counts/timestamps,
server preferences and the owner's key in Red Config, not conversation history.
Red user-data deletion cancels the user's request, removes statistics/cooldown/UI
state, and invalidates the response cache. It does not remove Discord messages or
provider-held data. Raw provider bodies and credentials are not shown in errors.

Run from the repository root in an environment with Red, pytest and pytest-asyncio:

```text
python -m pytest grokcog/tests --import-mode=importlib -q -o asyncio_default_fixture_loop_scope=function
python -m ruff check grokcog
python -m ruff format --check grokcog
```

Tests mock Discord/network boundaries and exercise real command registration, reply
context, search metadata, malformed output, concurrent requests, retries and cleanup.
Live deployment still needs the setup checks and an actual mention/reply/slash test.

If a search returns no usable sources, the cog logs a `search_no_sources` warning
under `red.grokcog`, containing only counts of tool/result fields. It does not log
your question, quoted messages, URLs, tool output or key. Such answers are not
cached, so a retry can recover immediately. The footer distinguishes an unverified
search from a normal chat answer. Website-visit evidence in `browser_results` is
accepted alongside search results. Embed footers and all 25 possible fields are
included in reply context, subject to the total context bound above.

API references:
- [Groq models](https://console.groq.com/docs/models)
- [Groq web search and response metadata](https://console.groq.com/docs/tool-use/built-in-tools/web-search)
- [Compound tool configuration](https://console.groq.com/docs/compound/built-in-tools)
- [Qwen image input](https://console.groq.com/docs/vision)
