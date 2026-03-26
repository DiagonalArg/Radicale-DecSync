#!/usr/bin/env python3

import contextlib
import json
import os
import vobject

from radicale import pathutils
from radicale import item as radicale_item
import radicale.storage.multifilesystem as storage
from libdecsync import Decsync

def _get_attributes_from_path(path):
    attributes = pathutils.strip_path(path).split("/")
    if not attributes[0]:
        attributes.pop()
    return attributes

class CollectionHrefMappingsMixin:
    def load_hrefs(self, sync_type):
        if sync_type == "contacts":
            self._suffix = ".vcf"
        else:
            self._suffix = ".ics"
        self._hrefs_path = os.path.join(self._filesystem_path, ".Radicale.hrefs")
        try:
            with open(self._hrefs_path) as f:
                self._hrefs = json.load(f)
        except:
            self._hrefs = {}
        self._uids = {}
        for uid, href in self._hrefs.items():
            self._uids[href] = uid

    def get_href(self, uid):
        return self._hrefs.get(uid, uid + self._suffix)

    def set_href(self, uid, href):
        if href == self.get_href(uid):
            return
        self._hrefs[uid] = href
        self._uids[href] = uid
        with self._atomic_write(self._hrefs_path, "w") as f:
            json.dump(self._hrefs, f)

    def get_uid(self, href):
        return self._uids.get(href, href[:-len(self._suffix)])

class Collection(storage.Collection, CollectionHrefMappingsMixin):
    def __init__(self, storage_, path, filesystem_path=None):
        super().__init__(storage_, path, filesystem_path=filesystem_path)
        attributes = _get_attributes_from_path(path)
        if len(attributes) == 2:
            decsync_dir = self._storage.decsync_dir
            sync_type = attributes[1].split("-")[0]
            if sync_type not in ["contacts", "calendars", "tasks", "memos"]:
                raise RuntimeError("Unknown sync type " + sync_type)
            collection = attributes[1][len(sync_type)+1:]
            own_app_id = Decsync.get_app_id("Radicale")
            self.decsync = Decsync(decsync_dir, sync_type, collection, own_app_id)

            def info_listener(path, datetime, key, value, extra):
                if key == "name":
                    extra._set_meta_key("D:displayname", value, update_decsync=False)
                elif key == "deleted":
                    if value:
                        extra.delete(update_decsync=False)
                elif key == "color":
                    extra._set_meta_key("ICAL:calendar-color", value, update_decsync=False)
                else:
                    raise ValueError("Unknown info key " + key)
            self.decsync.add_listener(["info"], info_listener)

            def resources_listener(path, datetime, key, value, extra):
                if len(path) != 1:
                    raise ValueError("Invalid path " + str(path))
                uid = path[0]
                href = extra.get_href(uid)
                if value is None:
                    if extra._get(href) is not None:
                        extra.delete(href, update_decsync=False)
                else:
                    vobject_item = vobject.readOne(value)
                    if sync_type == "contacts":
                        tag = "VADDRESSBOOK"
                    else:
                        tag = "VCALENDAR"
                    radicale_item.check_and_sanitize_items([vobject_item], tag=tag)
                    item = radicale_item.Item(collection=extra, vobject_item=vobject_item, uid=uid)
                    item.prepare()
                    extra.upload(href, item, update_decsync=False)
            self.decsync.add_listener(["resources"], resources_listener)

            self.load_hrefs(sync_type)

    def upload(self, href, orig_item, update_decsync=True):
        result = super().upload(href, orig_item)
        if isinstance(result, tuple):
            item = next((x for x in result if hasattr(x, "uid")), result[1])
        else:
            item = result
        if update_decsync:
            if hasattr(self, 'decsync'):
                tag = self.get_meta("tag")
                if tag == "VCALENDAR":
                    supported = (self.get_meta("C:supported-calendar-component-set") or "VEVENT,VTODO,VJOURNAL").split(",")
                    if len(supported) > 1 and item.component_name != "VEVENT":
                        raise RuntimeError("Component not supported by old DecSync.")
                self.set_href(item.uid, href)
                # Ensure structured data (lists/tuples) are stringified before serialization
                for component in item.vobject_item.getChildren():
                    if isinstance(component.value, (list, tuple)):
                        component.value = ";".join(str(x) for x in component.value)
                self.decsync.set_entry(["resources", item.uid], None, item.serialize())
        return result

    def delete(self, href=None, update_decsync=True):
        if update_decsync:
            if href is None:
                self.decsync.set_entry(["info"], "deleted", True)
            else:
                uid = self.get_uid(href)
                self.decsync.set_entry(["resources", uid], None, None)
        super().delete(href)

    def set_meta(self, props, update_decsync=True):
        for key, value in props.items():
            old_value = self.get_meta(key)
            if old_value == value:
                continue
            if key == "D:displayname":
                if update_decsync:
                    self.decsync.set_entry(["info"], "name", value)
            elif key == "ICAL:calendar-color":
                if update_decsync:
                    self.decsync.set_entry(["info"], "color", value)
            elif key == "C:supported-calendar-component-set" and old_value != None:
                # Changing the supported components is not allowed
                props[key] = old_value
        super().set_meta(props)

    def _set_meta_key(self, key, value, update_decsync=True):
        props = self.get_meta()
        props[key] = value
        self.set_meta(props, update_decsync)

    @property
    def etag(self):
        self.decsync.execute_all_new_entries(self)
        return super().etag

    def sync(self, old_token=None):
        if hasattr(self, "decsync"):
            self.decsync.execute_all_new_entries(self)
        return super().sync(old_token)

class Storage(storage.Storage):
    _collection_class = Collection

    def __init__(self, configuration):
        super().__init__(configuration)
        try:
            self.decsync_dir = os.path.expanduser(configuration.get("storage", "decsync_dir"))
        except KeyError:
            self.decsync_dir = ""

    def discover(self, path, depth="0", child_context_manager=(
            lambda path, href=None: contextlib.ExitStack()),user_groups=set()):
        collections = list(super().discover(path, depth, child_context_manager, user_groups))
        for collection in collections:
            yield collection

        if depth == "0":
            return

        attributes = _get_attributes_from_path(path)

        if len(attributes) == 0:
            return
        elif len(attributes) == 1:
            username = attributes[0]
            known_paths = []
            for collection in collections:
                if isinstance(collection, tuple):
                    collection = next((x for x in collection if hasattr(x, "path")), collection[0])
                known_paths.append(collection.path)
            for sync_type in ["contacts", "calendars", "tasks", "memos"]:
                for collection in Decsync.list_collections(self.decsync_dir, sync_type):
                    child_path = "/%s/%s-%s/" % (username, sync_type, collection)
                    if pathutils.strip_path(child_path) in known_paths:
                        continue
                    if Decsync.get_static_info(self.decsync_dir, sync_type, collection, "deleted") == True:
                        continue

                    props = {}
                    if sync_type == "contacts":
                        props["tag"] = "VADDRESSBOOK"
                    else:
                        props["tag"] = "VCALENDAR"
                        if sync_type == "calendars":
                            props["C:supported-calendar-component-set"] = "VEVENT"
                        elif sync_type == "tasks":
                            props["C:supported-calendar-component-set"] = "VTODO"
                        elif sync_type == "memos":
                            props["C:supported-calendar-component-set"] = "VJOURNAL"
                        else:
                            raise RuntimeError("Unknown sync type " + sync_type)
                    result = super().create_collection(child_path, props=props)
                    if isinstance(result, tuple):
                        child = next((x for x in result if hasattr(x, "decsync")), result[0])
                    else:
                        child = result
                    child.decsync.init_stored_entries()
                    child.decsync.execute_stored_entries_for_path_exact(["info"], child)
                    child.decsync.execute_stored_entries_for_path_prefix(["resources"], child)
                    yield child
        elif len(attributes) == 2:
            return
        else:
            raise ValueError("Invalid number of attributes")

    def move(self, item, to_collection, to_href):
        raise NotImplementedError

    def create_collection(self, href, items=None, props=None):
        attributes = _get_attributes_from_path(href)
        if props is None or len(attributes) != 2:
            return super().create_collection(href, items, props)
        username, collection = attributes

        tag = props.get("tag")
        if tag == "VADDRESSBOOK":
            sync_type = "contacts"
        elif tag == "VCALENDAR":
            components = props.get("C:supported-calendar-component-set")
            if components is None:
                if collection.startswith("calendars-"):
                    components = "VEVENT"
                elif collection.startswith("tasks-"):
                    components = "VTODO"
                elif collection.startswith("memos-"):
                    components = "VJOURNAL"
                else:
                    # Default
                    components = "VEVENT"
            components = components.split(",")
            component = components[0]
            if component == "VEVENT":
                sync_type = "calendars"
            elif component == "VTODO":
                sync_type = "tasks"
            elif component == "VJOURNAL":
                sync_type = "memos"
            else:
                raise RuntimeError("Unknown component " + component)
            props["C:supported-calendar-component-set"] = component
            for extra_component in components[1:]:
                tmp_props = props.copy()
                tmp_props["C:supported-calendar-component-set"] = extra_component
                self.create_collection(href, items, tmp_props)
        else:
            raise RuntimeError("Unknown tag " + tag)

        if collection.startswith(sync_type + "-"):
            path = href 
        else:
            path = "/%s/%s-%s/" % (username, sync_type, collection)
        result = super().create_collection(path, None, props)
        if isinstance(result, tuple):
            col = next((x for x in result if hasattr(x, "get_href")), result[0])
        else:
            col = result
        if items is not None:
            for item in items:
                href = col.get_href(item.uid)
                col.upload(href, item)    
        return result
