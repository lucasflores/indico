# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from itertools import count

from flask import flash, jsonify, request, session
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from indico.core.db import db
from indico.core.db.sqlalchemy.util.models import get_default_values
from indico.modules.events.controllers.base import RHDisplayEventBase
from indico.modules.events.layout import layout_settings, logger
from indico.modules.events.layout.forms import MenuBuiltinEntryForm, MenuLinkForm, MenuPageForm
from indico.modules.events.layout.models.menu import EventPage, MenuEntry, MenuEntryType
from indico.modules.events.layout.util import menu_entries_for_event
from indico.modules.events.layout.views import WPMenuEdit, WPPage
from indico.modules.events.management.controllers import RHManageEventBase
from indico.modules.events.models.events import EventType
from indico.modules.logs import EventLogRealm, LogKind
from indico.modules.logs.util import make_diff_log
from indico.util.i18n import _
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import url_for
from indico.web.forms.base import FormDefaults
from indico.web.util import jsonify_data, jsonify_form


entry_log_fields = {
    'title': {'title': 'Title', 'type': 'string'},
    'is_enabled': 'Show',
    'new_tab': 'New Tab',
    'protection_mode': 'Protection mode',
    'speakers_can_access': 'Grant speakers access',
    'link_url': {'title': 'URL', 'type': 'string'},
    'html': 'Content',
}


def _render_menu_entry(entry):
    tpl = get_template_module('events/layout/_menu.html')
    return tpl.menu_entry(entry=entry)


def _render_menu_entries(event, connect_menu=False):
    tpl = get_template_module('events/layout/_menu.html')
    return tpl.menu_entries(menu_entries_for_event(event), connect_menu=connect_menu)


class RHMenuBase(RHManageEventBase):
    def _check_access(self):
        RHManageEventBase._check_access(self)
        if self.event.type_ != EventType.conference:
            raise NotFound('Only conferences have a menu')

    def _require_custom_menu(self):
        if not layout_settings.get(self.event, 'use_custom_menu'):
            raise Forbidden('The menu cannot be changed unless menu customization is enabled')


class RHMenuEdit(RHMenuBase):
    def _process(self):
        custom_menu_enabled = layout_settings.get(self.event, 'use_custom_menu')
        menu = menu_entries_for_event(self.event) if custom_menu_enabled else None
        return WPMenuEdit.render_template('menu_edit.html', self.event, menu=menu,
                                          custom_menu_enabled=custom_menu_enabled)


class RHMenuToggleCustom(RHMenuBase):
    def _process(self):
        enabled = request.form['enabled'] == '1'
        if enabled:
            # nothing else to do here. menu items are added to the DB when retrieving the menu
            flash(_('Menu customization has been enabled.'), 'success')
        else:
            for entry in MenuEntry.query.with_parent(self.event):
                db.session.delete(entry)
            flash(_('Menu customization has been disabled.'), 'success')
        layout_settings.set(self.event, 'use_custom_menu', enabled)
        logger.info('Menu customization for %s %s by %s', self.event, 'enabled' if enabled else 'disabled',
                    session.user)
        if enabled:
            self.event.log(EventLogRealm.management, LogKind.positive, 'Menu', 'Custom menu enabled', session.user)
        else:
            self.event.log(EventLogRealm.management, LogKind.negative, 'Menu', 'Custom menu disabled', session.user)
        return jsonify(enabled=enabled)


class RHMenuEntryEditBase(RHMenuBase):
    normalize_url_spec = {
        'locators': {
            lambda self: self.entry
        }
    }

    def _check_access(self):
        RHMenuBase._check_access(self)
        self._require_custom_menu()

    def _process_args(self):
        RHMenuBase._process_args(self)
        self.entry = MenuEntry.get_or_404(request.view_args['menu_entry_id'])


class RHMenuEntryEdit(RHMenuEntryEditBase):
    def _process(self):
        defaults = FormDefaults(self.entry)
        form_kwargs = {}
        if self.entry.is_user_link:
            form_cls = MenuLinkForm
        elif self.entry.is_page:
            form_cls = MenuPageForm
            form_kwargs['editor_upload_url'] = url_for('attachments.upload_editor', self.event)
            defaults['html'] = self.entry.page.html
        else:
            form_cls = MenuBuiltinEntryForm
            defaults = FormDefaults(self.entry, skip_attrs={'title'},
                                    title=self.entry.title or self.entry.default_data.title,
                                    custom_title=self.entry.title is not None)
        form = form_cls(entry=self.entry, obj=defaults, event=self.event, **form_kwargs)
        if form.validate_on_submit():
            changes = self.entry.populate_from_dict(form.data, skip={'html', 'custom_title'})

            if self.entry.is_page and self.entry.page.html != form.html.data:
                changes['html'] = (self.entry.page.html, form.html.data)
                self.entry.page.html = form.html.data

            self.entry.log(EventLogRealm.management, LogKind.change, 'Menu',
                           f'Menu entry changed: {self.entry.log_title}', session.user,
                           data={'Changes': make_diff_log(changes, entry_log_fields)})

            return jsonify_data(entry=_render_menu_entry(self.entry))
        return jsonify_form(form)


class RHMenuEntryPosition(RHMenuEntryEditBase):
    def _process(self):
        position = request.form.get('position')
        try:
            position = int(position)
        except (TypeError, ValueError):
            position = None

        parent_id = request.form.get('parent_id')
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = None

        if parent_id != self.entry.parent_id:
            if self.entry.type not in {MenuEntryType.user_link, MenuEntryType.page}:
                raise BadRequest(
                    f'Menu entry "{self.entry}" cannot be moved to another menu: Invalid type "{self.entry.type.name}".'
                )
            if self.entry.is_root and self.entry.children:
                raise BadRequest(
                    f'Menu entry "{self.entry}" cannot be moved to another menu: Entry has nested entries.'
                )

            parent_entry = None
            if parent_id is not None:
                parent_entry = (MenuEntry.query.with_parent(self.event)
                                .filter(MenuEntry.type.in_({MenuEntryType.user_link, MenuEntryType.page}),
                                        MenuEntry.id == parent_id,
                                        MenuEntry.parent_id.is_(None))
                                .first())
                if not parent_entry:
                    raise BadRequest(f'New parent entry not found for Menu entry "{self.entry}".')

            self.entry.insert(parent_entry, position)
        else:
            self.entry.move(position)

        self.entry.log(EventLogRealm.management, LogKind.change, 'Menu', f'Menu entry moved: {self.entry.log_title}',
                       session.user, data={'Parent': self.entry.parent.log_title if self.entry.parent else None,
                                           'Position': self.entry.position + 1})
        return jsonify_data(flash=False)


class RHMenuEntryToggleEnabled(RHMenuEntryEditBase):
    def _process(self):
        self.entry.is_enabled = not self.entry.is_enabled
        if self.entry.is_enabled:
            self.entry.log(EventLogRealm.management, LogKind.positive, 'Menu',
                           f'Menu entry enabled: {self.entry.log_title}', session.user)
        else:
            self.entry.log(EventLogRealm.management, LogKind.negative, 'Menu',
                           f'Menu entry disabled: {self.entry.log_title}', session.user)
        return jsonify(is_enabled=self.entry.is_enabled)


class RHMenuEntryToggleDefault(RHMenuEntryEditBase):
    def _process(self):
        if self.entry.type != MenuEntryType.page:
            raise BadRequest
        if self.event.default_page == self.entry.page:
            is_default = False
            self.event.default_page = None
            self.entry.log(EventLogRealm.management, LogKind.change, 'Menu',
                            f'Event homepage unset: {self.entry.log_title}', session.user)
        else:
            is_default = True
            self.event.default_page = self.entry.page
            self.entry.log(EventLogRealm.management, LogKind.change, 'Menu',
                            f'Event homepage set: {self.entry.log_title}', session.user)
        return jsonify(is_default=is_default)


class RHMenuAddEntry(RHMenuBase):
    def _check_access(self):
        RHMenuBase._check_access(self)
        self._require_custom_menu()

    def _process(self):
        defaults = FormDefaults(get_default_values(MenuEntry))
        form_kwargs = {}
        entry_type = request.args['type']

        if entry_type == MenuEntryType.separator.name:
            entry = MenuEntry(event=self.event, type=MenuEntryType.separator)
            db.session.add(entry)
            db.session.flush()
            entry.log(EventLogRealm.management, LogKind.positive, 'Menu', f'Menu entry added: {entry.log_title}',
                      session.user)
            return jsonify_data(flash=False, entry=_render_menu_entry(entry))

        elif entry_type == MenuEntryType.user_link.name:
            form_cls = MenuLinkForm
        elif entry_type == MenuEntryType.page.name:
            form_cls = MenuPageForm
            form_kwargs['editor_upload_url'] = url_for('attachments.upload_editor', self.event)
        else:
            raise BadRequest

        form = form_cls(obj=defaults, event=self.event, **form_kwargs)
        if form.validate_on_submit():
            entry = MenuEntry(event=self.event, type=MenuEntryType[entry_type])
            changes = entry.populate_from_dict(form.data, skip={'html'})
            if entry.is_page:
                page = EventPage(html=form.html.data)
                self.event.custom_pages.append(page)
                entry.page = page
                changes['html'] = ('', page.html)

            db.session.add(entry)
            db.session.flush()

            entry.log(EventLogRealm.management, LogKind.positive, 'Menu', f'Menu entry added: {entry.log_title}',
                      session.user, data={'Changes': make_diff_log(changes, entry_log_fields)})
            return jsonify_data(entry=_render_menu_entry(entry))
        return jsonify_form(form)


class RHMenuDeleteEntry(RHMenuEntryEditBase):
    def _process(self):
        if self.entry.type not in (MenuEntryType.user_link, MenuEntryType.page, MenuEntryType.separator):
            raise BadRequest(f'Menu entry of type {self.entry.type.name} cannot be deleted')

        position_gen = count(self.entry.position)
        if self.entry.children:
            for child in self.entry.children:
                child.parent_id = self.entry.parent_id
                child.position = next(position_gen)

        with db.session.no_autoflush:
            entries = (MenuEntry.query.with_parent(self.event)
                       .filter(MenuEntry.parent_id == self.entry.parent_id,
                               MenuEntry.position >= self.entry.position,
                               MenuEntry.id != self.entry.id)
                       .order_by(MenuEntry.position)
                       .all())
        for entry in entries:
            entry.position = next(position_gen)

        self.entry.log(EventLogRealm.management, LogKind.negative, 'Menu',
                       f'Menu entry deleted: {self.entry.log_title}', session.user)
        db.session.delete(self.entry)
        db.session.flush()

        return jsonify_data(flash=False, menu=_render_menu_entries(self.event, connect_menu=True))


class RHPageDisplay(RHDisplayEventBase):
    normalize_url_spec = {
        'locators': {
            lambda self: self.page.locator.noslug,
        },
        'preserved_args': {'slug'},
    }

    normalize_url_spec_late = {
        'locators': {
            lambda self: self.page,
        }
    }

    def _check_access(self):
        RHDisplayEventBase._check_access(self)
        if not self.page.menu_entry.can_access(session.user):
            raise Forbidden

    def _process_args(self):
        RHDisplayEventBase._process_args(self)
        self.page = EventPage.get_or_404(request.view_args['page_id'])

    def _process(self):
        return WPPage.render_template('page.html', self.event, page=self.page)
