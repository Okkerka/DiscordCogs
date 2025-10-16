

    # Playlist Queueing Methods

    async def _queue_playlist_batch(
        self,
        ctx: commands.Context,
        tracks: List[Any],
        playlist_name: str,
        progress_msg: Optional[discord.Message] = None
    ) -> Dict[str, int]:
        """
        Queue multiple tracks from a playlist with progress updates.

        Parameters:
            ctx (commands.Context): Discord context.
            tracks (List[Any]): List of track objects.
            playlist_name (str): Name of the playlist for status messages.
            progress_msg (Optional[discord.Message]): Message to update with progress.

        Returns:
            Dict[str, int]: Dictionary with 'queued' and 'skipped' counts.
        """
        self._suppress_enqueued(ctx)
        queued = 0
        skipped = 0
        total = len(tracks)
        last_update = 0

        try:
            for i, track in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    break

                try:
                    if await self._play(ctx, track, show_embed=False):
                        queued += 1
                    else:
                        skipped += 1

                    if progress_msg and (i - last_update >= BATCH_UPDATE_INTERVAL or i == total):
                        try:
                            embed = discord.Embed(
                                title=Messages.PROGRESS_QUEUEING.format(
                                    name=playlist_name,
                                    count=total
                                ),
                                description=Messages.PROGRESS_UPDATE.format(
                                    queued=queued,
                                    skipped=skipped,
                                    current=i,
                                    total=total
                                ),
                                color=discord.Color.blue()
                            )
                            await progress_msg.edit(embed=embed)
                            last_update = i
                        except discord.HTTPException:
                            pass

                    await asyncio.sleep(0.1)

                except Exception as e:
                    log.error(f"Error queueing track {i}/{total}: {e}")
                    skipped += 1

            return {"queued": queued, "skipped": skipped, "total": total}

        finally:
            self._restore_send(ctx)
            await self._set_cancel(ctx.guild.id, False)

    async def _search_and_queue(
        self,
        ctx: commands.Context,
        query: str,
        track_name: str
    ) -> bool:
        """
        Search Tidal for a track and queue the best match.

        Parameters:
            ctx (commands.Context): Discord context.
            query (str): Search query string.
            track_name (str): Original track name for logging.

        Returns:
            bool: True if track was found and queued successfully.
        """
        try:
            tracks = await self._search_tidal(query)
            if not tracks:
                log.info(f"No Tidal match for: {track_name}")
                return False

            return await self._play(ctx, tracks[0], show_embed=False)

        except Exception as e:
            log.error(f"Search and queue failed for '{track_name}': {e}")
            return False

    async def _queue_spotify_playlist(
        self,
        ctx: commands.Context,
        playlist_id: str
    ) -> None:
        """
        Queue tracks from a Spotify playlist by searching on Tidal.

        Parameters:
            ctx (commands.Context): Discord context.
            playlist_id (str): Spotify playlist ID.
        """
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        if not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_SPOTIFY)

        try:
            playlist = await self.bot.loop.run_in_executor(
                None,
                self.sp.playlist,
                playlist_id
            )

            if not playlist or "tracks" not in playlist:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            tracks = playlist["tracks"]["items"]
            if not tracks:
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist")
                )
                return

            playlist_name = playlist.get("name", "Spotify Playlist")

            embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(count=len(tracks)),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.green()
            )
            await progress_msg.edit(embed=embed)

            queued = 0
            skipped = 0
            last_update = 0

            for i, item in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    embed = discord.Embed(
                        title=Messages.STATUS_STOPPING,
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(
                            queued=queued,
                            skipped=skipped
                        ),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                track = item.get("track")
                if not track:
                    skipped += 1
                    continue

                try:
                    track_name = track["name"]
                    artist_name = track["artists"][0]["name"] if track.get("artists") else ""
                    query = f"{track_name} {artist_name}"

                    if await self._search_and_queue(ctx, query, track_name):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(tracks):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(count=len(tracks)),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued,
                                skipped=skipped,
                                current=i,
                                total=len(tracks)
                            ),
                            color=discord.Color.green()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.1)

                except Exception as e:
                    log.error(f"Error processing Spotify track {i}: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued,
                    total=len(tracks),
                    skipped=skipped
                ),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.green()
            )
            await progress_msg.edit(embed=embed)

        except Exception as e:
            log.error(f"Spotify playlist queue error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)

        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_youtube_playlist(
        self,
        ctx: commands.Context,
        playlist_id: str
    ) -> None:
        """
        Queue tracks from a YouTube playlist by searching on Tidal.

        Parameters:
            ctx (commands.Context): Discord context.
            playlist_id (str): YouTube playlist ID.
        """
        if not YOUTUBE_API_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_YOUTUBE)
            return

        if not self.yt:
            await ctx.send(Messages.ERROR_NO_YOUTUBE)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_YOUTUBE)

        try:
            request = self.yt.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50
            )

            response = await self.bot.loop.run_in_executor(None, request.execute)

            if not response or "items" not in response:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            items = response["items"]
            if not items:
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist")
                )
                return

            playlist_title = items[0]["snippet"].get("playlistTitle", "YouTube Playlist")

            embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(count=len(items)),
                description=f"Playlist: {playlist_title}",
                color=discord.Color.red()
            )
            await progress_msg.edit(embed=embed)

            queued = 0
            skipped = 0
            last_update = 0

            for i, item in enumerate(items, 1):
                if await self._should_cancel(ctx.guild.id):
                    embed = discord.Embed(
                        title=Messages.STATUS_STOPPING,
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(
                            queued=queued,
                            skipped=skipped
                        ),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                try:
                    video_title = item["snippet"]["title"]

                    if await self._search_and_queue(ctx, video_title, video_title):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(items):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(count=len(items)),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued,
                                skipped=skipped,
                                current=i,
                                total=len(items)
                            ),
                            color=discord.Color.red()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.1)

                except Exception as e:
                    log.error(f"Error processing YouTube video {i}: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued,
                    total=len(items),
                    skipped=skipped
                ),
                description=f"Playlist: {playlist_title}",
                color=discord.Color.red()
            )
            await progress_msg.edit(embed=embed)

        except Exception as e:
            log.error(f"YouTube playlist queue error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)

        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        """
        Handle Tidal URLs for tracks, albums, or playlists.

        Parameters:
            ctx (commands.Context): Discord context.
            url (str): Tidal URL to process.
        """
        track_match = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url)
        album_match = re.search(r"tidal\.com/(?:browse/)?album/(\d+)", url)
        playlist_match = re.search(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)", url)

        try:
            if track_match:
                track_id = track_match.group(1)
                track = await self.bot.loop.run_in_executor(
                    None,
                    self.session.track,
                    track_id
                )
                if track:
                    await self._play(ctx, track)
                else:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

            elif album_match:
                album_id = album_match.group(1)
                album = await self.bot.loop.run_in_executor(
                    None,
                    self.session.album,
                    album_id
                )

                if not album:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, album.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="album"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(
                        name=album.name,
                        count=len(tracks)
                    )
                )

                result = await self._queue_playlist_batch(
                    ctx,
                    tracks,
                    album.name,
                    progress_msg
                )

                embed = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=result["queued"],
                        total=result["total"],
                        skipped=result["skipped"]
                    ),
                    description=f"Album: {album.name}",
                    color=discord.Color.blue()
                )
                await progress_msg.edit(embed=embed)

            elif playlist_match:
                playlist_id = playlist_match.group(1)
                playlist = await self.bot.loop.run_in_executor(
                    None,
                    self.session.playlist,
                    playlist_id
                )

                if not playlist:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, playlist.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(
                        name=playlist.name,
                        count=len(tracks)
                    )
                )

                result = await self._queue_playlist_batch(
                    ctx,
                    tracks,
                    playlist.name,
                    progress_msg
                )

                embed = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=result["queued"],
                        total=result["total"],
                        skipped=result["skipped"]
                    ),
                    description=f"Playlist: {playlist.name}",
                    color=discord.Color.blue()
                )
                await progress_msg.edit(embed=embed)

            else:
                await ctx.send(Messages.ERROR_INVALID_URL.format(
                    platform="Tidal",
                    content_type="track/album/playlist"
                ))

        except Exception as e:
            log.error(f"Tidal URL handling error: {e}", exc_info=True)
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)


    # Discord Commands

    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """
        Play a track or queue a playlist from Tidal, Spotify, or YouTube.

        Parameters:
            ctx (commands.Context): Discord context.
            query (str): Search query or URL (Tidal/Spotify/YouTube).

        Usage:
            >tplay <search query>
            >tplay <Tidal URL>
            >tplay <Spotify playlist URL>
            >tplay <YouTube playlist URL>
        """
        if not await self._check_ready(ctx):
            return

        tidal_url = re.search(r"tidal\.com/(?:browse/)?(track|album|playlist)", query)
        spotify_playlist = re.search(
            r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)",
            query
        )
        youtube_playlist = re.search(
            r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)",
            query
        )

        if tidal_url:
            await self._handle_tidal_url(ctx, query)

        elif spotify_playlist:
            playlist_id = spotify_playlist.group(1)
            await self._queue_spotify_playlist(ctx, playlist_id)

        elif youtube_playlist:
            playlist_id = youtube_playlist.group(1)
            await self._queue_youtube_playlist(ctx, playlist_id)

        else:
            try:
                tracks = await self._search_tidal(query)

                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
                    return

                await self._play(ctx, tracks[0])

            except APIError as e:
                await ctx.send(f"{Messages.ERROR_NO_TRACKS_FOUND} ({str(e)})")
            except Exception as e:
                log.error(f"Search error: {e}", exc_info=True)
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """
        Stop queueing a playlist.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tstop
        """
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx: commands.Context) -> None:
        """
        Display the current queue with track metadata.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tqueue
        """
        try:
            queue = await self.config.guild(ctx.guild).track_metadata()

            if not queue:
                await ctx.send(Messages.STATUS_EMPTY_QUEUE)
                return

            pages = []
            items_per_page = 10

            for i in range(0, len(queue), items_per_page):
                chunk = queue[i:i + items_per_page]
                description = ""

                for j, meta in enumerate(chunk, start=i + 1):
                    duration_str = self._format_time(meta["duration"])
                    quality_str = self._get_quality_label(meta["quality"])

                    description += (
                        f"**{j}.** {meta['title']}\n"
                        f"    {meta['artist']} • {duration_str} • {quality_str}\n"
                    )

                embed = discord.Embed(
                    title=f"Queue ({len(queue)} tracks)",
                    description=description,
                    color=discord.Color.blue()
                )
                embed.set_footer(
                    text=f"Page {len(pages) + 1}/{(len(queue) - 1) // items_per_page + 1}"
                )
                pages.append(embed)

            if len(pages) == 1:
                await ctx.send(embed=pages[0])
            else:
                await menu(ctx, pages, DEFAULT_CONTROLS)

        except Exception as e:
            log.error(f"Queue display error: {e}", exc_info=True)
            await ctx.send("Error displaying queue.")

    @commands.command(name="tclear")
    async def tclear(self, ctx: commands.Context) -> None:
        """
        Clear the queue metadata.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tclear
        """
        await self._clear_meta(ctx.guild.id)
        await ctx.send(Messages.SUCCESS_QUEUE_CLEARED)

    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        """
        Set up Tidal OAuth authentication.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalsetup

        Notes:
            This command initiates the OAuth flow. Follow the console
            instructions to complete authentication.
        """
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return

        try:
            login, future = await self.bot.loop.run_in_executor(
                None,
                self.session.login_oauth
            )

            if login.verify_link_expired:
                await ctx.send(Messages.PROGRESS_OAUTH)
                print(f"\n[TIDAL] Visit: {login.verification_uri_complete}")
                print(f"[TIDAL] Or enter code: {login.user_code} at {login.verification_uri}\n")

                try:
                    await asyncio.wait_for(future, timeout=300)
                except asyncio.TimeoutError:
                    await ctx.send("OAuth timeout. Please try again.")
                    return

                if self.session.check_login():
                    await self.config.token_type.set(self.session.token_type)
                    await self.config.access_token.set(self.session.access_token)
                    await self.config.refresh_token.set(self.session.refresh_token)

                    expiry = None
                    if self.session.expiry_time:
                        expiry = int(self.session.expiry_time.timestamp())
                    await self.config.expiry_time.set(expiry)

                    await ctx.send(Messages.SUCCESS_TIDAL_SETUP)
                else:
                    await ctx.send("Login failed. Please try again.")
            else:
                await ctx.send(Messages.PROGRESS_OAUTH_PENDING)

        except Exception as e:
            log.error(f"OAuth setup error: {e}", exc_info=True)
            await ctx.send(f"Setup failed: {str(e)}")

    @commands.group(name="tidalplay")
    @commands.is_owner()
    async def tidalplay(self, ctx: commands.Context) -> None:
        """
        Configure Spotify and YouTube API credentials.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalplay spotify <client_id> <client_secret>
            >tidalplay youtube <api_key>
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(
        self,
        ctx: commands.Context,
        client_id: str,
        client_secret: str
    ) -> None:
        """
        Configure Spotify API credentials.

        Parameters:
            ctx (commands.Context): Discord context.
            client_id (str): Spotify client ID.
            client_secret (str): Spotify client secret.

        Usage:
            >tidalplay spotify <client_id> <client_secret>
        """
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        try:
            await self.config.spotify_client_id.set(client_id)
            await self.config.spotify_client_secret.set(client_secret)

            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id,
                    client_secret
                )
            )

            await self.bot.loop.run_in_executor(
                None,
                lambda: self.sp.search("test", limit=1)
            )

            await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

        except Exception as e:
            log.error(f"Spotify configuration error: {e}", exc_info=True)
            await ctx.send(f"Spotify setup failed: {str(e)}")
            self.sp = None

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, api_key: str) -> None:
        """
        Configure YouTube API credentials.

        Parameters:
            ctx (commands.Context): Discord context.
            api_key (str): YouTube Data API key.

        Usage:
            >tidalplay youtube <api_key>
        """
        if not YOUTUBE_API_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_YOUTUBE)
            return

        try:
            await self.config.youtube_api_key.set(api_key)

            self.yt = build("youtube", "v3", developerKey=api_key)

            await ctx.send(Messages.SUCCESS_YOUTUBE_CONFIGURED)

        except Exception as e:
            log.error(f"YouTube configuration error: {e}", exc_info=True)
            await ctx.send(f"YouTube setup failed: {str(e)}")
            self.yt = None

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        """
        Clear stored Tidal OAuth tokens.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalplay cleartokens

        Notes:
            Use this if you encounter authentication issues.
        """
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)


    # Event Listeners

    @commands.Cog.listener()
    async def on_red_audio_track_start(
        self,
        guild: discord.Guild,
        track: Any,
        requester: discord.Member
    ) -> None:
        """
        Handle track start events from the Audio cog.

        Removes metadata for the started track from the queue.

        Parameters:
            guild (discord.Guild): Guild where track started.
            track (Any): Track object from Audio cog.
            requester (discord.Member): Member who requested the track.
        """
        try:
            await self._pop_meta(guild.id)
        except Exception as e:
            log.error(f"Track start event error for guild {guild.id}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_red_audio_queue_end(
        self,
        guild: discord.Guild,
        track_count: int,
        total_duration: int
    ) -> None:
        """
        Handle queue end events from the Audio cog.

        Clears all remaining metadata when the queue finishes.

        Parameters:
            guild (discord.Guild): Guild where queue ended.
            track_count (int): Number of tracks that were played.
            total_duration (int): Total duration of all tracks.
        """
        try:
            await self._clear_meta(guild.id)
        except Exception as e:
            log.error(f"Queue end event error for guild {guild.id}: {e}", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    """
    Register the TidalPlayer cog with the bot.

    Parameters:
        bot (commands.Bot): The Red-DiscordBot instance.
    """
    await bot.add_cog(TidalPlayer(bot))
