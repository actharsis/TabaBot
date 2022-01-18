import asyncio
import datetime
import emoji
import json
import os
import random
import time
import pixivpy3.utils

from PIL import Image
from config import pixiv_refresh_token, pixiv_show_embed_illust
from discord import Embed, File
from discord.colour import Colour
from discord.ext import commands
from discord_slash import cog_ext
from discord_slash.model import SlashCommandOptionType
from discord_slash.utils.manage_commands import create_option, create_choice
from io import BytesIO
from main import client
from modules.pixiv_auth import refresh_token
from pixivpy3 import *


def is_date(date_text):
    try:
        datetime.datetime.strptime(date_text, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False


def str_time_prop(start, end, time_format, prop):
    stime = time.mktime(time.strptime(start, time_format))
    etime = time.mktime(time.strptime(end, time_format))
    ptime = stime + prop * (etime - stime)
    return time.strftime(time_format, time.localtime(ptime))


def random_date(start, end, prop):
    return str_time_prop(start, end, '%Y-%m-%d', prop)


def current_date():
    return time.strftime('%Y-%m-%d', time.localtime(time.time()))


def next_year(date):
    ptime = time.mktime(time.strptime(date, '%Y-%m-%d'))
    ptime += 31536000
    return time.strftime('%Y-%m-%d', time.localtime(ptime))


def back_date(months):
    ptime = time.time()
    ptime -= months * 2678400
    return time.strftime('%Y-%m-%d', time.localtime(ptime))


class BetterAppPixivAPI(AppPixivAPI):
    def download(self, url, prefix='', path=os.path.curdir, name=None, replace=False, fname=None,
                 referer='https://app-api.pixiv.net/'):
        with self.requests_call('GET', url, headers={'Referer': referer}, stream=True) as response:
            return Image.open(response.raw)

    def search_autocomplete(self, word, req_auth=True):
        url = '%s/v2/search/autocomplete' % self.hosts
        params = {
            'word': word,
        }
        r = self.no_auth_requests_call('GET', url, params=params, req_auth=req_auth)
        return self.parse_result(r)


class PixivCog(commands.Cog):
    api = BetterAppPixivAPI()
    api.auth(refresh_token=pixiv_refresh_token)

    def __init__(self, bot):
        self.bot = bot
        self.channels = {}
        self.timers = {}
        self.last_query = None
        self.last_type = None
        self.token_expiration_time = None
        self.spoilers = False
        self.load()

    def save(self, channels=False, timers=False):
        if channels:
            with open('json/auto_pixiv_channels.json', 'w') as file:
                file.write(json.dumps(self.channels))
        if timers:
            with open('json/auto_pixiv_timers.json', 'w') as file:
                file.write(json.dumps(self.timers))

    def load(self):
        try:
            with open('json/auto_pixiv_channels.json', 'r') as file:
                self.channels = json.load(file)
            with open('json/auto_pixiv_timers.json', 'r') as file:
                self.timers = json.load(file)
        except:
            pass

    async def show_page(self, query, channel, limit=30, minimum_views=None, minimum_rate=None):
        if channel is None or query.illusts is None or len(query.illusts) == 0:
            return 0, 0, False
        shown = 0
        print('fetched', len(query.illusts), 'images')
        color = Colour.random()
        for illust in query.illusts:
            if shown == limit:
                break
            if minimum_views is not None and illust.total_view < minimum_views:
                continue
            if minimum_rate is not None and illust.total_bookmarks / illust.total_view * 100 < minimum_rate:
                continue
            filename = f'{str(illust.id)}.png'
            if self.spoilers and illust.sanity_level >= 6:
                filename = f'SPOILER_{filename}'
            title = illust.title
            with BytesIO() as image_binary:
                img = self.api.download(illust.image_urls.large)
                img.save(image_binary, 'PNG')
                image_binary.seek(0)
                file = File(fp=image_binary, filename=filename)
                if pixiv_show_embed_illust:
                    embed = Embed(description=f'Title: [{title}](https://www.pixiv.net/en/artworks/{illust.id})',
                                  color=color)
                    embed.set_image(url=f'attachment://{filename}')
                    message = await channel.send(embed=embed, file=file)
                else:
                    message = await channel.send(f'Title: {title}', file=File(fp=image_binary, filename=filename))
                shown += 1
            if illust.is_bookmarked:
                await message.add_reaction(emoji.emojize(':red_heart:'))
        return shown, len(query.illusts), True

    async def show_page_embed(self, query, query_type, chat, limit=None, save_query=True):
        shown, total, result = await self.show_page(query, chat, limit)
        if result:
            if save_query:
                self.last_query = query
                self.last_type = query_type
            return Embed(title="Illustrations loaded", color=Colour.green())
        else:
            return Embed(title="Pixiv API can't process the request", color=Colour.red())

    @cog_ext.cog_slash(name="start_auto_pixiv", description="Add channel to the auto Pixiv list",
                       options=[
                           create_option(
                               name="refresh_time",
                               description="Delay between auto update in minutes (default = 180)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           ),
                           create_option(
                               name="limit",
                               description="Amount of pictures will be displayed (default = 20)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           )])
    async def add_auto_pixiv(self, ctx, refresh_time=180, limit=20):
        channel_id = str(ctx.channel.id)
        if channel_id in self.channels.keys():
            embed = Embed(title="Auto Pixiv already running on this channel", color=Colour.gold())
        else:
            self.channels[channel_id] = {"refresh_time": refresh_time * 60, "limit": limit}
            embed = Embed(title="Auto Pixiv is now running on this channel", color=Colour.green())
        await ctx.send(embed=embed)
        self.save(channels=True)

    @cog_ext.cog_slash(name="stop_auto_pixiv", description="Delete channel from the auto Pixiv list")
    async def delete_auto_pixiv(self, ctx):
        channel_id = str(ctx.channel.id)
        if channel_id in self.channels.keys():
            self.channels.pop(channel_id)
            if channel_id in self.timers.keys():
                self.timers.pop(channel_id)
            embed = Embed(title="Channel has been deleted", color=Colour.green())
        else:
            embed = Embed(title="This channel not exist in the list", color=Colour.gold())
        await ctx.send(embed=embed)
        self.save(channels=True, timers=True)

    @cog_ext.cog_slash(name="spoil_nsfw", description="Spoil NSFW image switch")
    async def change_spoiler(self, ctx):
        self.spoilers = not self.spoilers
        if self.spoilers:
            embed = Embed(title="NSFW pictures now will be spoiled", color=Colour.green())
        else:
            embed = Embed(title="NSFW spoiler feature turned off", color=Colour.red())
        await ctx.send(embed=embed, delete_after=5.0)

    @cog_ext.cog_slash(name='next', description="Show next page from the last 'best' query",
                       options=[
                           create_option(
                               name="limit",
                               description="Amount of pictures will be displayed (default = 10)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           )])
    async def next(self, ctx, limit=10):
        await ctx.defer()
        if self.last_query is not None:
            next_qs = self.api.parse_qs(self.last_query.next_url)
            query = None
            if self.last_type == 'recommended':
                query = self.api.illust_recommended(**next_qs)
            elif self.last_type == 'best':
                query = self.api.illust_ranking(**next_qs)
            embed = await self.show_page_embed(query, self.last_type, ctx.channel, limit, save_query=True)
        else:
            embed = Embed(title="Previous request not found", color=Colour.red())
        await ctx.send(embed=embed, delete_after=5.0)

    @cog_ext.cog_slash(name='recommended', description="Show [10] recommended Pixiv illustrations",
                       options=[
                           create_option(
                               name="limit",
                               description="Amount of pictures will be displayed (default = 10)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           )])
    async def recommended(self, ctx, limit=10):
        await ctx.defer()
        query = self.api.illust_recommended()
        embed = await self.show_page_embed(query, 'recommended', ctx.channel, limit, save_query=True)
        await ctx.send(embed=embed, delete_after=5.0)

    @cog_ext.cog_slash(name='best', description='Find best [10] rated illutstrations with specific mode and date',
                       options=[
                           create_option(
                               name="mode",
                               description="Specify one of the types",
                               option_type=SlashCommandOptionType.STRING,
                               required=False,
                               choices=[
                                   create_choice("day", "Day"),
                                   create_choice("week", "Week"),
                                   create_choice("month", "Month"),
                                   create_choice("day_male", "Day Male likes"),
                                   create_choice("day_female", "Day Female likes"),
                                   create_choice("day_r18", "Day Ecchi"),
                                   create_choice("day_male_r18", "Day Male likes Ecchi"),
                                   create_choice("week_r18", "Week Ecchi"),
                               ]
                           ),
                           create_option(
                               name="limit",
                               description="Amount of pictures will be displayed (default = 10)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False
                           ),
                           create_option(
                               name="date",
                               description="Date of sample in format: YYYY-MM-DD",
                               option_type=SlashCommandOptionType.STRING,
                               required=False
                           )
                       ])
    async def best(self, ctx, mode='month', limit=10, date=None):
        await ctx.defer()
        query = self.api.illust_ranking(mode=mode, date=date, offset=None)
        embed = await self.show_page_embed(query, 'best', ctx.channel, limit, save_query=True)
        await ctx.send(embed=embed, delete_after=5.0)

    @cog_ext.cog_slash(name='when', description='Show remaining time until new illustrations')
    async def time_to_update(self, ctx):
        channel_id = str(ctx.channel.id)
        if channel_id in self.timers.keys() and channel_id in self.channels:
            refresh_time = self.channels[channel_id]['refresh_time']
            last_update_time = self.timers[channel_id]
            delta = int(refresh_time + last_update_time - time.time())
            embed = Embed(title=str(delta // 60) + ' min ' + str(delta % 60) + ' secs until autopost',
                          color=Colour.green())
        else:
            embed = Embed(title="Timer has not started", color=Colour.gold())
        await ctx.send(embed=embed, delete_after=10.0)

    @cog_ext.cog_slash(name='tag', description='Get tagged name of word',
                       options=[
                           create_option(
                               name="word",
                               description="Word, that will be translated to most popular related tag",
                               option_type=SlashCommandOptionType.STRING,
                               required=True
                           )
                       ])
    async def tag(self, ctx, word):
        query = self.api.search_autocomplete(word)
        tags = ''
        for i, tag in enumerate(query.tags):
            tags += str(i) + '. ' + tag.name
            if tag.translated_name is not None:
                tags += ' - ' + tag.translated_name
            tags += '\n'
        embed = Embed(title="Tags on word '" + word + "' sorted from high to low popularity:",
                      description=tags, color=Colour.gold())
        await ctx.send(embed=embed, delete_after=30.0)

    @cog_ext.cog_slash(name='find', description='Find illustrations that satisfy the filters from random point of time',
                       options=[
                           create_option(
                               name="word",
                               description="Find by specific word (default = 猫耳)",
                               option_type=SlashCommandOptionType.STRING,
                               required=False,
                           ),
                           create_option(
                               name="match",
                               description="Word match rule (default = partial_match_for_tags)",
                               option_type=SlashCommandOptionType.STRING,
                               required=False,
                               choices=[
                                   create_choice("partial_match_for_tags", "partial_match_for_tags"),
                                   create_choice("exact_match_for_tags", "exact_match_for_tags"),
                                   create_choice("title_and_caption", "title_and_caption")
                               ]
                           ),
                           create_option(
                               name="limit",
                               description="Maximum amount of pictures that will be shown (default 5, maximum = 20)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           ),
                           create_option(
                               name="views",
                               description="Required minimum amount of views (default = 6000)",
                               option_type=SlashCommandOptionType.INTEGER,
                               required=False,
                           ),
                           create_option(
                               name="rate",
                               description="Required minimum percent of views/bookmarks (default = 3)",
                               option_type=SlashCommandOptionType.FLOAT,
                               required=False,
                           ),
                           create_option(
                               name="period",
                               description="Random Date period (no impact if since date given)",
                               option_type=SlashCommandOptionType.STRING,
                               required=False,
                               choices=[
                                   create_choice(back_date(1 * 12), "new"),
                                   create_choice(back_date(2 * 12), "2 years range"),
                                   create_choice(back_date(3 * 12), "3 years range"),
                                   create_choice(back_date(6 * 12), "6 years range"),
                                   create_choice(back_date(9 * 12), "9 years range"),
                                   create_choice(back_date(14 * 12), "all time period"),
                               ]
                           ),
                           create_option(
                               name="since_date",
                               description="Fixed date in format YYYY-MM-DD from which search will be initialized",
                               option_type=SlashCommandOptionType.STRING,
                               required=False,
                           )])
    async def find(self, ctx, word='猫耳', match='exact_match_for_tags',
                   limit=5, views=6000, rate=3.0, period=back_date(4 * 12), since_date=None):
        await ctx.defer()
        channel = ctx.channel
        try:
            word = self.api.search_autocomplete(word).tags[0].name
        except:
            pass
        limit = min(limit, 20)
        date = random_date(period, current_date(), random.random())
        if is_date(since_date):
            date = since_date
        fetched, shown, offset, alive = 0, 0, 0, True
        while shown < limit and alive and fetched < 3000:
            query = self.api.search_illust(word, search_target=match,
                                           end_date=date, offset=offset)
            if len(query.illusts) == 0 and date < current_date():
                date = next_year(date)
                offset = 0
                continue
            good, total, alive = await self.show_page(query, channel, limit - shown, views, rate)
            shown += good
            fetched += total
            if alive:
                offset += total
        embed = Embed(title="Find with word " + word + " called",
                      description=str(fetched) + " images fetched in total",
                      color=Colour.green())
        await ctx.send(embed=embed, delete_after=10.0)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        try:
            demojized = emoji.demojize(reaction.emoji)
        except TypeError:
            demojized = None
        if not reaction.me:
            if reaction.message.attachments is not None and len(reaction.message.attachments) == 1 and \
                    reaction.message.author == client.user:
                illust_id = reaction.message.attachments[0].filename.split('.')[0].replace("SPOILER_", "")
                emojis = {':red_heart:', ':growing_heart:', ':magnifying_glass_tilted_left:', ':seedling:',
                          ':broken_heart:', ':red_question_mark:', ':elephant:', ':face_vomiting:'}
                if demojized in emojis:
                    await reaction.remove(user)

                if demojized == ':red_heart:' or demojized == ':elephant:':
                    self.api.illust_bookmark_add(illust_id)
                    await reaction.message.add_reaction(emoji.emojize(':red_heart:'))
                elif demojized == ':growing_heart:':
                    self.api.illust_bookmark_add(illust_id)
                    await reaction.message.add_reaction(emoji.emojize(':red_heart:'))
                    query = self.api.illust_related(illust_id)
                    await self.show_page(query, reaction.message.channel, limit=5)
                elif demojized == ':magnifying_glass_tilted_left:':
                    try:
                        illust = self.api.illust_detail(illust_id).illust
                        with BytesIO() as image_binary:
                            filename = f'{str(illust.id)}.png'
                            if self.spoilers and illust.sanity_level >= 6:
                                filename = f'SPOILER_{filename}'
                            img = self.api.download(illust.meta_single_page.original_image_url)
                            img.save(image_binary, 'PNG')
                            image_binary.seek(0)
                            file = File(fp=image_binary, filename=filename)
                            await reaction.message.channel.send('Original quality', file=file)
                        await reaction.message.add_reaction(emoji.emojize(':thumbs_up:'))
                    except pixivpy3.utils.PixivError:
                        await reaction.message.add_reaction(emoji.emojize(':thumbs_down:'))
                elif demojized == ':seedling:':
                    await reaction.message.add_reaction(emoji.emojize(':thumbs_up:'))
                    query = self.api.illust_related(illust_id)
                    await self.show_page(query, reaction.message.channel, limit=5)
                elif demojized == ':face_vomiting:':
                    await reaction.message.delete()
                elif demojized == ':broken_heart:':
                    self.api.illust_bookmark_delete(illust_id)
                    for r in reaction.message.reactions:
                        if r.me:
                            await r.remove(client.user)
                    await reaction.message.add_reaction(emoji.emojize(':broken_heart:'))
                elif demojized == ':red_question_mark:':
                    await reaction.message.add_reaction(emoji.emojize(':thumbs_up:'))
                    detail = self.api.illust_bookmark_detail(illust_id)
                    msg = "Tags: "
                    for tag in detail.bookmark_detail.tags:
                        msg += tag.name + ', '
                    msg = msg[:-2]
                    await reaction.message.reply(msg, delete_after=30.0)
        elif demojized in [':broken_heart:', ':thumbs_up:', ':thumbs_down:']:
            await asyncio.sleep(5)
            await reaction.remove(user)

    async def auto_draw(self):
        try:
            for channel_id, options in self.channels.items():
                channel = self.bot.get_channel(int(channel_id))
                if channel is None:
                    continue
                refresh_time = options['refresh_time']
                limit = options['limit']
                timestamp = time.time()
                if channel_id not in self.timers.keys() or timestamp - self.timers[channel_id] > refresh_time:
                    self.timers[str(channel_id)] = timestamp
                    query = self.api.illust_recommended()
                    await self.show_page(query, channel, limit=limit)
                    self.save(timers=True)
        except RuntimeError:
            pass

    async def auto_refresh_token(self):
        timestamp = time.time()
        if self.token_expiration_time is None or self.token_expiration_time - timestamp < 1000:
            token, ttl = refresh_token(pixiv_refresh_token)
            self.token_expiration_time = timestamp + ttl
            self.api.auth(refresh_token=token)
            print('pixiv token updated')

    @commands.Cog.listener()
    async def on_ready(self):
        while True:
            await asyncio.sleep(10)
            await self.auto_draw()
            await self.auto_refresh_token()


def setup(bot):
    bot.add_cog(PixivCog(bot))