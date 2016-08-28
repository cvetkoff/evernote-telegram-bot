import importlib
import inspect
import json
import os
import sys
from os.path import realpath, dirname, join, basename

import aiomcache

import settings
from bot.model import User, ModelNotFound, TelegramUpdate, DownloadTask
from ext.evernote.client import EvernoteClient
from ext.telegram.bot import TelegramBot, TelegramBotCommand
from ext.telegram.models import Message


def get_commands(cmd_dir=None):
    commands = []
    if cmd_dir is None:
        cmd_dir = join(realpath(dirname(__file__)), 'commands')
    exclude_modules = ['__init__']
    for dirpath, dirnames, filenames in os.walk(cmd_dir):
        if basename(dirpath) == 'tests':
            continue
        for filename in filenames:
            file_path = join(dirpath, filename)
            ext = file_path.split('.')[-1]
            if ext not in ['py']:
                continue
            sys_path = list(sys.path)
            sys.path.insert(0, cmd_dir)
            module_name = inspect.getmodulename(file_path)
            if module_name not in exclude_modules:
                module = importlib.import_module(module_name)
                sys.path = sys_path
                for name, klass in inspect.getmembers(module):
                    if inspect.isclass(klass) and\
                       issubclass(klass, TelegramBotCommand) and\
                       klass != TelegramBotCommand:
                            commands.append(klass)
    return commands


class EvernoteBot(TelegramBot):

    def __init__(self, token, name):
        super(EvernoteBot, self).__init__(token, name)
        self.evernote = EvernoteClient(
            settings.EVERNOTE['key'],
            settings.EVERNOTE['secret'],
            settings.EVERNOTE['oauth_callback'],
            sandbox=settings.DEBUG
        )
        self.cache = aiomcache.Client("127.0.0.1", 11211)
        for cmd_class in get_commands():
            self.add_command(cmd_class)

    async def list_notebooks(self, user: User):
        key = "list_notebooks_{0}".format(user.id).encode()
        data = await self.cache.get(key)
        if not data:
            access_token = user.evernote_access_token
            notebooks = [{'guid': nb.guid, 'name': nb.name} for nb in
                         self.evernote.list_notebooks(access_token)]
            await self.cache.set(key, json.dumps(notebooks).encode())
        else:
            notebooks = json.loads(data.decode())
        return notebooks

    async def update_notebooks_cache(self, user):
        key = "list_notebooks_{0}".format(user.user_id).encode()
        access_token = user.evernote_access_token
        notebooks = [{'guid': nb.guid, 'name': nb.name} for nb in
                     self.evernote.list_notebooks(access_token)]
        await self.cache.set(key, json.dumps(notebooks).encode())

    # async def get_user(self, message):
    #     try:
    #         user = User.get({'user_id': message['from']['id']})
    #         if user.telegram_chat_id != message['chat']['id']:
    #             user.telegram_chat_id = message['chat']['id']
    #             user.save()
    #         return user
    #     except ModelNotFound:
    #         self.logger.warn("User %s not found" % message['from']['id'])

    async def set_current_notebook(self, user, notebook_name):
        all_notebooks = await self.list_notebooks(user)
        for notebook in all_notebooks:
            if notebook['name'] == notebook_name:
                user.current_notebook = notebook
                user.state = None
                user.save()

                if user.mode == 'one_note':
                    note_guid = self.evernote.create_note(
                        user.evernote_access_token, text='',
                        title='Note for Evernoterobot',
                        notebook_guid=notebook['guid'])
                    user.places[user.current_notebook['guid']] = note_guid
                    user.save()

                await self.api.sendMessage(
                    user.telegram_chat_id,
                    'From now your current notebook is: %s' % notebook_name,
                    reply_markup=json.dumps({'hide_keyboard': True}))
                break
        else:
            await self.api.sendMessage(user.telegram_chat_id,
                                       'Please, select notebook')

    async def set_mode(self, user, mode):
        text_mode = mode
        if mode.startswith('> ') and mode.endswith(' <'):
            mode = mode[2:-2]
        mode = mode.replace(' ', '_').lower()

        await self.api.sendMessage(
            user.telegram_chat_id,
            'From now this bot in mode "{0}"'.format(mode),
            reply_markup=json.dumps({'hide_keyboard': True}))

        user.mode = mode
        user.state = None
        user.save()

        if user.mode == 'one_note':
            reply = await self.api.sendMessage(
                user.telegram_chat_id, 'Please wait')
            note_guid = self.evernote.create_note(
                user.evernote_access_token, text='',
                title='Note for Evernoterobot')
            user.places[user.current_notebook['guid']] = note_guid
            user.save()

            text = 'Bot switched to mode "{0}". New note was created'.format(text_mode)
            await self.api.editMessageText(
                user.telegram_chat_id, reply["message_id"], text)

    async def accept_request(self, user: User, request_type: str, data):
        # TODO: get user_id instead of user
        reply = await self.api.sendMessage(user.telegram_chat_id,
                                           '🔄 Accepted')
        TelegramUpdate.create(user_id=user.id,
                              request_type=request_type,
                              status_message_id=reply['message_id'],
                              data=data)

    async def on_text(self, message: Message):
        user = User.get({'id': message.user.id})
        text = message.text
        if user.state == 'select_notebook':
            if text.startswith('> ') and text.endswith(' <'):
                text = text[2:-2]
            await self.set_current_notebook(user, text)
        elif user.state == 'switch_mode':
            await self.set_mode(user, text)
        else:
            await self.accept_request(user, 'text', message)

    async def on_photo(self, message: Message):
        user = User.get({'id': message.user.id})
        await self.accept_request(user, 'photo', message)
        files = sorted(message['photo'], key=lambda x: x.get('file_size'),
                       reverse=True)
        DownloadTask.create(user_id=user.id,
                            file_id=files[0]['file_id'],
                            file_size=files[0]['file_size'],
                            completed=False)

    async def on_video(self, message: Message):
        user = User.get({'id': message.user.id})
        await self.accept_request(user, 'video', message)
        video = message['video']
        DownloadTask.create(user_id=user.id,
                            file_id=video['file_id'],
                            file_size=video['file_size'],
                            completed=False)

    async def on_document(self, message: Message):
        user = User.get({'id': message.user.id})
        await self.accept_request(user, 'document', message)
        document = message['document']
        DownloadTask.create(user_id=user.id,
                            file_id=document['file_id'],
                            file_size=document['file_size'],
                            completed=False)

    async def on_voice(self, message: Message):
        user = User.get({'id': message.user.id})
        await self.accept_request(user, 'voice', message)
        voice = message['voice']
        DownloadTask.create(user_id=user.id,
                            file_id=voice['file_id'],
                            file_size=voice['file_size'],
                            completed=False)

    async def on_location(self, message: Message):
        user = User.get({'id': message.user.id})
        await self.accept_request(user, 'location', message)
