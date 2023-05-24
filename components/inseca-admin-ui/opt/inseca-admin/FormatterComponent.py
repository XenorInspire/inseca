# This file is part of INSECA.
#
#    Copyright (C) 2020-2022 INSECA authors
#
#    INSECA is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    INSECA is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with INSECA.  If not, see <https://www.gnu.org/licenses/>

import os
import datetime
import json
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GObject
from gi.repository import Gtk

import MiscUI as mui
import Configurations
import Jobs as jobs
import Utils as util
import PluggedDevices as pdev

class Params(GObject.Object):
    """Object to manage the parameters which must be provided to format a storage device
    and the associated widgets in @ui"""
    __gsignals__ = {
        "data_changed": (GObject.SIGNAL_RUN_FIRST, None, ())
    }
    def __init__(self, gconf, dconf, fconf):
        GObject.Object.__init__(self)
        if not isinstance(gconf, Configurations.GlobalConfiguration):
            raise Exception("CODEBUG: invalid @gconf argument")
        if not isinstance(dconf, Configurations.DomainConfig):
            raise Exception("CODEBUG: invalid @dconf argument")
        if not isinstance(fconf, Configurations.FormatConfig):
            raise Exception("CODEBUG: invalid @iconf argument")
        self._gconf=gconf
        self._dconf=dconf
        self._fconf=fconf

        self._init_parameters_sets()

        self._links={}

    def _init_parameters_sets(self):
        """Initialize the parameters sets:
        - self._core_params: imposed by INSECA
        - self._fconf_params:  imposed by the format configuration
        """
        core_params=self._fconf.parameters_core.copy()

        # "core" parameters which should be defined by the user (the others are defined automatically)
        self._fconf_params={}
        for pname in ["password-user", "fs-data", "enctype-data"]:
            self._fconf_params[pname]=core_params[pname]
            del core_params[pname]

        self._core_params=core_params
        self._fconf_params.update(self._fconf.parameters_config)
        #print("FCONF params ==> %s"%json.dumps(self._fconf_params, indent=4))

    def _generate_core_parameters(self):
        """Generate all the CORE parameters, from random values or fixed/contextual information"""
        res={}
        # fs
        res["fs-data"]="exfat"
        # config ID
        res["confid"]=self._fconf.id
        # creation-date
        now=datetime.datetime.utcnow()
        ts=int(datetime.datetime.timestamp(now))
        res["creation-date"]=datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        # creation-date-ts
        res["creation-date-ts"]=ts
        # password-rescue
        res["password-rescue"]=self._fconf.password_rescue
        # device-signing-private-key-file
        res["device-signing-private-key-file"]=os.path.basename(self._fconf.devicemeta_privkey)
        return res

    def _compute_link_name(self, params_set, component, pname):
        if params_set==self._core_params or params_set==self._fconf_params:
            return "||%s"%pname
        else:
            return "userdata|%s|%s"%(component, pname)

    def _get_widget_for_param(self, pname):
        lname=self._compute_link_name(self._fconf_params, None, pname)
        if lname in self._links:
            return self._links[lname]
        return None

    def create_widgets(self, builder):
        """Create the actual labels and entry widgets for all the parameters"""
        top=2
        grid=builder.get_object("format-grid")

        # config params
        for pname in self._fconf_params:
            if pname not in self._fconf.overrides:
                pspec=self._fconf_params[pname]
                label=mui.create_label_widget(pspec)
                grid.attach(label, 0, top, 1, 1)
                widget=mui.create_param_entry(pspec)
                widget.connect("data_changed", self._data_changed_cb)
                self._links[self._compute_link_name(self._fconf_params, None, pname)]=widget
                grid.attach(widget, 1, top, 1, 1)
                top+=1
        grid.show_all()

    def _data_changed_cb(self, widget):
        self.emit("data_changed")

    def get_format_valued_params(self):
        """Returns all the parameters required to format a device"""
        # core and from UI
        res=self._generate_core_parameters()
        for pname in self._fconf_params:
            widget=self._get_widget_for_param(pname)
            if widget:
                value=widget.get_value()
                if value=="":
                    raise Exception("%s: invalid empty value"%pname)
                res[pname]=value

        # overrides
        overrides=self._fconf.overrides
        for pname in overrides:
            if pname in res:
                res[pname]=overrides[pname]

        return res


#
# Formatting UI
#
class Component:
    """Format a storage device"""
    def __init__(self, ui, builder):
        self._ui=ui
        self._builder=builder
        self._error_message=self._builder.get_object("format-error")
        self._error_message.hide()
        self._page_widget=self._builder.get_object("format")
        self._form_grid=self._builder.get_object("format-grid")
        self._templates={} # key=@self._combo_template text, value: the associated [domain config, format config] objects list
        self._combo_template=self._builder.get_object("format-template")
        self._combo_template.connect("changed", self._format_template_changed_cb)
        self._cancel_button=self._builder.get_object("cancel-button")
        self._back_button=self._builder.get_object("back-button")

        # combo for the list of plugged devices
        self._combo_device=pdev.DevicesListUI(self._ui.plugged_devices_obj)
        self._form_grid.attach(self._combo_device, 1, 0, 1, 1)
        self._combo_device.show()
        self._combo_device.connect("changed", self._params_changed_cb)

        self._internal_page_change=False

        # attributes updated when a format configuration is chosen via the UI
        self._dconf=None
        self._fconf=None
        self._params=None # will be a Params object when a format configuration has been chosen
        self._final_params=None
        self._devfile=None

        # widgets in the form which need to be kept when the form changes (when the format config changes)
        self._to_keep=[self._combo_device]
        for label in ("format-device-label", "format-template", "format-template-label"):
            self._to_keep+=[self._builder.get_object(label)]

        # extra widgets
        bbox=builder.get_object("actions-bbox")
        self._format_button=Gtk.Button(label="Format")
        bbox.add(self._format_button)
        self._format_button.connect("clicked", self._format_device)
        self._format_button.set_sensitive(False)

    def page_shown_cb(self, page_widget):
        if page_widget==self._page_widget:
            self._format_button.show()
            if self._internal_page_change:
                return
            self._update_format_templates()
        else:
            self._format_button.hide()

    def _update_format_templates(self):
        """Update the combo box to select among the list of format configurations available, and
        update the associated self._templates"""
        combo=self._combo_template
        current_template=combo.get_active_text()
        combo.remove_all()
        self._templates={}
        index=0
        add_dom_prefix=True
        gconf=Configurations.get_gconf()
        if len(gconf.domain_configs)==1:
            add_dom_prefix=False
        for duid in gconf.domain_configs:
            dconf=gconf.get_domain_conf(duid)
            for iuid in dconf.format_ids:
                iconf=gconf.get_format_conf(iuid)
                if add_dom_prefix:
                    text="%s - %s"%(dconf.descr, iconf.descr)
                else:
                    text=iconf.descr
                combo.append_text(text)
                self._templates[text]=[dconf, iconf]
                if current_template==iconf.descr:
                    combo.set_active(index)
                index+=1
        if index==1:
            combo.set_active(0)

    def _format_template_changed_cb(self, widget):
        """Called when the selected format template has changed"""
        self._fconf=None
        current_template=self._combo_template.get_active_text()
        if current_template is not None:
            (self._dconf, self._fconf)=self._templates[current_template]
        self._update_form()

    def _update_form(self):
        """Reset the format form (called after the format template has changed)"""
        # remove all children of self._form_grid
        def torem(child, container):
            if child not in self._to_keep:
                container.remove(child)
        self._form_grid.foreach(torem, self._form_grid)
        if not self._fconf:
            return

        # get the actual format informations
        self._internal_page_change=True
        self._ui.show_page("message")
        try:
            gconf=Configurations.get_gconf()
            self._params=Params(gconf, self._dconf, self._fconf)
            self._params.create_widgets(self._builder)
            self._params.connect("data_changed", self._params_changed_cb)
            self._params_changed_cb(None)
        except Exception as e:
            self._ui.show_error(str(e))
        finally:
            self._back_button.set_sensitive(True)
            self._ui.show_page("format")
            self._internal_page_change=False

    def _params_changed_cb(self, dummy):
        """Update the UI while the form elements are modified by the end user"""
        self._final_params=None
        self._devfile=None
        if self._params is None:
            # no format configuration selected
            return
        try:
            pvalues=self._params.get_format_valued_params()
            self._devfile=self._combo_device.get_selected_devfile()
            if self._devfile is None:
                raise Exception("No device selected")

            self._final_params=pvalues
            self._error_message.hide()
            self._format_button.set_sensitive(True)
        except Exception as e:
            self._error_message.set_text(str(e))
            self._error_message.show()
            self._format_button.set_sensitive(False)

    def _format_device(self, dummy):
        """Actually format a device"""
        #print("FORMAT CONF: %s"%json.dumps(self._final_params, indent=4))
        #print("on: %s"%self._devfile)

        self._internal_page_change=True
        self._ui.show_page("message")
        try:
            sid=None # safe init value
            params_file=util.Temp(data=json.dumps(self._final_params))
            if False:
                # debug, to be removed
                self._final_params["password-user"]="ChocolatChoco12"
                open("/tmp/DEBUG-format.json", "w").write(json.dumps(self._final_params))
            args=["--verbose", "dev-format", self._fconf.id, params_file.name, self._devfile]
            job=jobs.InsecaRunJob(args, "Formatting device", feedback_component=self._ui.feedback_component)
            job.start()

            self._cancel_button.show()
            sid=self._cancel_button.connect("clicked", self._cancel_job, job)
            self._back_button.set_sensitive(False)

            job.wait_with_ui()
            if job.exception is not None:
                util.print_event("Failed: %s"%str(job.exception))
                raise job.exception
            self._ui.update_plugged_devices() # refresh the status of the plugged devices
        except Exception as e:
            self._ui.show_error(str(e))
            self._ui.show_page("format")
        finally:
            self._back_button.set_sensitive(True)
            self._cancel_button.hide()
            if sid is not None:
                self._cancel_button.disconnect(sid)
            self._ui.show_page("format")
            self._internal_page_change=False
            self._update_form()

    def _cancel_job(self, widget, job):
        job.cancel()