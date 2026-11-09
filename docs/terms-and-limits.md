# Terms and limits

fairshare routes between accounts **you own and pay for**. By design it:

- never reads, copies, stores or transmits a login, token or session cookie. Each account
  stays signed in through the provider's own flow in its own config directory;
- never switches account inside a running session;
- uses only data the provider hands to a user-configured command (for Claude Code, the
  documented `statusLine` input). It calls no undocumented endpoint.

**Not settled, and yours to decide.** Providers' terms may treat deliberately spreading load
across several subscriptions differently from ordinary use. At the time of writing, Anthropic's
Claude Code legal page says advertised limits "assume ordinary, individual usage" and its Usage
Policy prohibits intentionally bypassing restrictions; neither addresses holding two personal
subscriptions. Read the current terms of every provider you use. This project gives no legal
advice, and its authors accept no responsibility for how it is used.

fairshare is an independent project. It is not affiliated with or endorsed by Anthropic,
OpenAI or any other provider.
