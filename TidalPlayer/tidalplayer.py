from redbot.core import commands, Config
import aiohttp
import logging
import asyncio

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Search Tidal first, play from YouTube with fallback"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_global = {
            "bearer_token": None,
            "refresh_token": None,
            "client_id": None,
            "client_secret": None,
            "country_code": "US"
        }
        self.config.register_global(**default_global)
    
    @commands.group()
    @commands.is_owner()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth authentication"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()
    
    @tidalsetup.command(name="oauth")
    async def setup_oauth(self, ctx):
        """Interactive OAuth setup guide"""
        
        setup_msg = """
**Tidal OAuth Setup Guide**

**Step 1:** Go to https://developer.tidal.com and sign in
**Step 2:** Create a new app or select an existing one
**Step 3:** Copy your **Client ID** and **Client Secret**
**Step 4:** Add `http://localhost:8080` to your app's redirect URIs

Reply with your **Client ID** (you have 60 seconds):
        """
        
        await ctx.send(setup_msg)
        
        # Get Client ID
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        
        try:
            client_id_msg = await self.bot.wait_for('message', timeout=60.0, check=check)
            client_id = client_id_msg.content.strip()
            await client_id_msg.delete()
            
            await ctx.send("✅ Client ID received!\n\nNow reply with your **Client Secret**:")
            
            # Get Client Secret
            client_secret_msg = await self.bot.wait_for('message', timeout=60.0, check=check)
            client_secret = client_secret_msg.content.strip()
            await client_secret_msg.delete()
            
            # Save credentials
            await self.config.client_id.set(client_id)
            await self.config.client_secret.set(client_secret)
            
            await ctx.send("✅ Credentials saved!\n\nNow generating authorization URL...")
            
            # Generate auth URL
            auth_url = f"https://login.tidal.com/authorize?response_type=code&client_id={client_id}&redirect_uri=http://localhost:8080&scope=r_usr+w_usr+w_sub"
            
            auth_msg = f"""
**Step 5:** Click this URL and authorize the app:
{auth_url}

**Step 6:** After authorizing, you'll be redirected to a localhost page. Copy the **code** parameter from the URL.
Example: `http://localhost:8080?code=ABC123...`

Reply with the **authorization code**:
            """
            
            await ctx.send(auth_msg)
            
            # Get auth code
            auth_code_msg = await self.bot.wait_for('message', timeout=120.0, check=check)
            auth_code = auth_code_msg.content.strip()
            await auth_code_msg.delete()
            
            await ctx.send("🔄 Exchanging code for tokens...")
            
            # Exchange code for tokens
            success = await self.exchange_code_for_token(client_id, client_secret, auth_code)
            
            if success:
                await ctx.send("✅ **Setup complete!** You can now use `>play` to search Tidal and play from YouTube.")
            else:
                await ctx.send("❌ Failed to get tokens. Please try again or set manually with `>tidaltoken`")
                
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Setup timed out. Please try again with `>tidalsetup oauth`")
        except Exception as e:
            await ctx.send(f"❌ Error during setup: {e}")
            log.error(f"OAuth setup error: {e}")
    
    async def exchange_code_for_token(self, client_id: str, client_secret: str, auth_code: str):
        """Exchange authorization code for access and refresh tokens"""
        
        data = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://localhost:8080",
            "client_id": client_id,
            "client_secret": client_secret
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://auth.tidal.com/v1/oauth2/token",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        
                        # Save tokens
                        await self.config.bearer_token.set(tokens.get("access_token"))
                        await self.config.refresh_token.set(tokens.get("refresh_token"))
                        
                        return True
                    else:
                        error_text = await resp.text()
                        log.error(f"Token exchange failed: {resp.status} - {error_text}")
                        return False
        except Exception as e:
            log.error(f"Error exchanging token: {e}")
            return False
    
    @tidalsetup.command(name="refresh")
    async def refresh_token_cmd(self, ctx):
        """Refresh your access token using refresh token"""
        
        client_id = await self.config.client_id()
        client_secret = await self.config.client_secret()
        refresh_token = await self.config.refresh_token()
        
        if not all([client_id, client_secret, refresh_token]):
            await ctx.send("❌ Missing credentials! Run `>tidalsetup oauth` first.")
            return
        
        success = await self.refresh_access_token()
        
        if success:
            await ctx.send("✅ Access token refreshed successfully!")
        else:
            await ctx.send("❌ Failed to refresh token. You may need to run `>tidalsetup oauth` again.")
    
    async def refresh_access_token(self):
        """Refresh the access token"""
        
        client_id = await self.config.client_id()
        client_secret = await self.config.client_secret()
        refresh_token = await self.config.refresh_token()
        
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://auth.tidal.com/v1/oauth2/token",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        await self.config.bearer_token.set(tokens.get("access_token"))
                        return True
                    else:
                        log.error(f"Token refresh failed: {resp.status}")
                        return False
        except Exception as e:
            log.error(f"Error refreshing token: {e}")
            return False
    
    @commands.command()
    async def play(self, ctx, *, query: str):
        """Play from Tidal first, YouTube as fallback"""
        
        bearer_token = await self.config.bearer_token()
        
        if not bearer_token:
            await ctx.send("⚠️ No Tidal token set! Run `>tidalsetup oauth` first, or searching YouTube directly...")
            search_query = query
        else:
            # Try Tidal first
            async with ctx.typing():
                tidal_result = await self.search_tidal(query, bearer_token)
            
            if tidal_result:
                # Found on Tidal - use metadata for YouTube search
                search_query = f"{tidal_result['artist']} {tidal_result['title']}"
                await ctx.send(f"🎵 Found on Tidal: **{tidal_result['title']}** by **{tidal_result['artist']}**\nSearching YouTube...")
            else:
                # Not found on Tidal - fallback to direct search
                search_query = query
                await ctx.send("🔍 Not found on Tidal, searching YouTube directly...")
        
        # Use Audio cog to play from YouTube
        audio = self.bot.get_cog("Audio")
        if not audio:
            await ctx.send("❌ Audio cog is not loaded!")
            return
        
        # Call Audio's play command
        await audio.command_play(ctx, query=search_query)
    
    async def search_tidal(self, query: str, bearer_token: str):
        """Search Tidal catalog using OAuth Bearer token"""
        country_code = await self.config.country_code()
        
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json"
        }
        
        params = {
            "query": query,
            "limit": 1,
            "countryCode": country_code
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://openapi.tidal.com/search",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Check if we have track results
                        if data.get("tracks") and data["tracks"].get("items"):
                            track = data["tracks"]["items"][0]
                            return {
                                "title": track.get("title", "Unknown"),
                                "artist": track.get("artists", [{}])[0].get("name", "Unknown") if track.get("artists") else "Unknown"
                            }
                    elif resp.status == 401:
                        # Token expired, try to refresh
                        log.info("Token expired, attempting refresh...")
                        if await self.refresh_access_token():
                            # Retry with new token
                            return await self.search_tidal(query, await self.config.bearer_token())
                    else:
                        log.warning(f"Tidal API returned status {resp.status}")
        except Exception as e:
            log.error(f"Error searching Tidal: {e}")
        
        return None
    
    @commands.command()
    @commands.is_owner()
    async def tidaltoken(self, ctx, token: str):
        """Manually set your Tidal OAuth Bearer token"""
        await self.config.bearer_token.set(token)
        await ctx.send("✅ Tidal Bearer token has been set!")
        try:
            await ctx.message.delete()
        except:
            await ctx.send("⚠️ Please delete your message containing the token!")
    
    @commands.command()
    @commands.is_owner()
    async def tidalcountry(self, ctx, country_code: str):
        """Set Tidal country code (e.g., US, GB, DE)"""
        await self.config.country_code.set(country_code.upper())
        await ctx.send(f"✅ Tidal country code set to: {country_code.upper()}")
