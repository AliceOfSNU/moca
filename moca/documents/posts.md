### Writing a Post
Operator-requested only: 모카 never writes a post because a member asked.

- `python -m chatbot.post --category 가입인사 "요청"` — 모카 (GPT-6 Astra, `post_prompt()`) drafts a title (≤40 chars) and body
  from the request, the profile and `moca_capabilities.md`. The draft is saved to `data/posts/<timestamp>.json` and shown;
  it's published only after confirmation (`--yes` skips the prompt).
- `--dry-run` fills the write screen, verifies title/body/category from the UI tree, then clears it and leaves.
  `--draft data/posts/<file>.json --yes` publishes a saved draft exactly as checked.
- Posting (`somoim/board.py: SomoimBoard.write`): 게시판 → 작성 → category dialog → title (`title_edit`) and body
  (`editor`, a contenteditable inside a WebView). The app autosaves drafts and restores them next time, so both fields
  are cleared before typing and before discarding. After 완료, the post title must appear on the board.
- Editing (`SomoimBoard.edit`): only the author sees 편집 → 게시글 수정 on a post, so 모카 can edit only its own posts.
  The edit form is the write form without the category dialog; after 완료 the post must show the new title and body.
  Refresh the saved copy afterwards (`read_post` + `save_post`) — body edits aren't caught until a full re-sync.
- The body editor doesn't render markdown: plain lines, blank lines, `- ` lists and emoji only.
- Categories: 자유 글, 관심사 공유, 모임후기, 가입인사, 공지사항(전체알림) — this one notifies every member —, 투표.

### Reading new posts and remembering them
when a new post appears, read its content and save it to a file
(we don't need a llm for this. for now, we can just ignore any pictures)
the reason we save all posts is because we want to be able to efficiently search through them.
For example, the user may ask '가입인사 양식이 뭐야?' and the model should be able to answer based on the 공지사항.

So the model gets a grep_search tool and a partial(scoped) read tool on posts.
There is a sample implementation under this directory, taken from a different unrelated project. You can reuse this.

#### Implementation
- **Storage** (`chatbot/posts.py`): one file per post, `data/board/<category>/<YYYY-MM-DD_HHMM>_<author>_<title>.md`:
  a `# title` line, 작성자 (role), 작성일, 카테고리, 필독 공지, then the body text. `data/board/index.json` lists every
  saved post with its board-list title/time (used to find it again) and `last_full_sync`.
- **Reading** (`somoim/board.py`): the board list gives author, time, title, category and a body preview per card;
  pinned [필독] notices show only a title. A post body is either an `editor` node holding the whole text or an
  `editor`/`rich_editor` container of text pieces (pictures are Image nodes and are skipped).
  A post keeps its author and posting time when edited, so those identify a saved post (the title only breaks ties
  within the same minute). Pinned notices show only a title in the list, so they're matched by title, and a renamed
  notice replaces its old copy by the author and time read from the post itself.
- **Sync** (`sync_posts`): new-post notifications (101 "새글", 102 "모임에 게시글을 작성했습니다") and the 30-minute
  fallback save posts not saved yet and re-read posts whose list title changed, scanning the board newest-first until
  the first post saved with the same title. Edits send no notification.
  Every `--post-resync` seconds (default 3 hours) a full sync also re-reads posts whose title, category or preview changed,
  always re-reads pinned notices (no preview in the list), and removes posts no longer on the board.
  Comments are not saved. Manual run: `python -m chatbot.posts sync [--full]`.
- **Tools** (`chatbot/post_tools.py`, adapted from `fops.py`, scoped to `data/board`): `list_posts(category, author)`,
  `grep_search(pattern, path, context)` (case-insensitive, with surrounding lines) and `read_file(path, start_line, end_line)`
  (unranged reads limited to 150 lines). Both the 모임 chat 모카 (Astra) and the 1:1 모카 (Sol) get them, with
  `POST_SEARCH_RULES` in their prompts: search before answering board-type questions, name the post used, and don't
  spread personal details from 가입인사 posts to others unless needed.
- 가입인사 greetings now come from the saved index instead of a separate listing of the 가입인사 board.
