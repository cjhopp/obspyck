#!/usr/bin/env python
# -*- coding: utf-8 -*-
#-------------------------------------------------------------------
# Filename: obspyck.py
#  Purpose: ObsPyck main program
#   Author: Tobias Megies, Lion Krischer
#    Email: megies@geophysik.uni-muenchen.de
#  License: GPLv2
#
# Copyright (C) 2010 Tobias Megies, Lion Krischer
#---------------------------------------------------------------------
import locale
import logging
import optparse
import os
import re
import shutil
import socket
import sys
import tempfile
import warnings
from collections import OrderedDict
from configparser import SafeConfigParser, NoOptionError, NoSectionError
from io import BytesIO

from PyQt5 import QtGui, QtCore, QtWidgets
from PyQt5.QtCore import Qt
import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.cm
import matplotlib.transforms
import requests
from datetime import datetime
from matplotlib.cm import get_cmap
from matplotlib.patches import Ellipse
from matplotlib.ticker import FuncFormatter, FormatStrFormatter, MaxNLocator
from matplotlib.backend_bases import MouseEvent as MplMouseEvent

#sys.path.append('/baysoft/obspy/misc/symlink')
#os.chdir("/baysoft/obspyck/")
import obspy
import obspy.imaging.cm as obspy_cm
from obspy import UTCDateTime, Stream, Trace
from obspy.core.event import CreationInfo, WaveformStreamID, \
    OriginUncertainty, OriginQuality, Comment, NodalPlane, NodalPlanes
from obspy.core.util import NamedTemporaryFile, AttribDict
from obspy.geodetics.base import gps2dist_azimuth, kilometer2degrees
from obspy.signal.util import util_lon_lat
from obspy.signal.invsim import estimate_magnitude
from obspy.signal.rotate import rotate_zne_lqt, rotate_ne_rt
from obspy.signal.trigger import ar_pick
from obspy.imaging.spectrogram import spectrogram
from obspy.imaging.beachball import beach
from obspy.clients.seishub import Client as SeisHubClient

from . import __version__
from .qt_designer import Ui_qMainWindow_obsPyck
from .util import (
    _save_input_data, fetch_waveforms_with_metadata, matplotlib_color_to_rgb,
    SplitWriter, LOGLEVELS, setup_external_programs, connect_to_server,
    merge_check_and_cleanup_streams, cleanup_streams_without_metadata,
    MultiCursor, WIDGET_NAMES, coords2azbazinc, map_rotated_channel_code,
    ONSET_CHARS, POLARITY_CHARS, COMPONENT_COLORS, formatXTicklabels,
    AXVLINEWIDTH, PROGRAMS, getArrivalForPick, POLARITY_2_FOCMEC, gk2lonlat,
    errorEllipsoid2CartesianErrors, readNLLocScatter, ONE_SIGMA, VERSION_INFO,
    MAG_MARKER, getPickForArrival, COMMANDLINE_OPTIONS, set_matplotlib_defaults,
    check_keybinding_conflicts, surf_xyz2latlon)
from .event_helper import Catalog, Event, Origin, Pick, Arrival, \
    Magnitude, StationMagnitude, StationMagnitudeContribution, \
    FocalMechanism, ResourceIdentifier, ID_ROOT, readQuakeML, Amplitude, \
    merge_events_in_catalog

NAMESPACE = "http://erdbeben-in-bayern.de/xmlns/0.1"
NSMAP = {"edb": NAMESPACE}

ICON_PATH = os.path.join(os.path.dirname(
    sys.modules[__name__].__file__), 'obspyck{}.gif')

if list(map(int, obspy.__version__.split('.')[:2])) < [1, 1]:
    msg = "Needing ObsPy version >= 1.1.0 (current version is: {})"
    warnings.warn(msg.format(obspy.__version__))


class ObsPyck(QtWidgets.QMainWindow):
    """
    Main Window with the design loaded from the Qt Designer.
    """
    def __init__(self, clients, streams, options, keys, config, inventories):
        """
        Standard init.
        """
        self.clients = clients
        self.streams = streams
        self.options = options
        self.keys = keys
        self.config = config

        # make a mapping of seismic phases to colors as specified in config
        self.seismic_phases = OrderedDict(config.items('seismic_phases'))
        self._magnitude_color = config.get('base', 'magnitude_pick_color')

        # TREF is the global reference time (zero in relative time scales)
        if options.time is not None:
            self.TREF = UTCDateTime(options.time)
        else:
            self.TREF = UTCDateTime(config.get("base", "time"))
        if options.starttime_offset is not None:
            self.T0 = self.TREF + options.starttime_offset
        else:
            self.T0 = self.TREF + config.getfloat("base", "starttime_offset")
            self.options.starttime_offset = config.getfloat("base",
                                                            "starttime_offset")
        # T1 is the end time specified by user
        if options.duration is not None:
            self.T1 = self.T0 + options.duration
        else:
            self.T1 = self.T0 + config.getfloat("base", "duration")

        # save username of current user
        try:
            self.username = os.getlogin()
        except:
            try:
                self.username = os.environ['USER']
            except:
                self.username = "unknown"

        # init the GUI stuff
        QtWidgets.QMainWindow.__init__(self)
        # Init the widgets from the autogenerated file.
        # All GUI elements will be accessible via self.widgets.name_of_element
        self.widgets = Ui_qMainWindow_obsPyck()
        self.widgets.setupUi(self)

        # set icon
        app_icon = QtGui.QIcon()
        app_icon.addFile(ICON_PATH.format("_16x16"), QtCore.QSize(16, 16))
        app_icon.addFile(ICON_PATH.format("_24x42"), QtCore.QSize(24, 24))
        app_icon.addFile(ICON_PATH.format("_32x32"), QtCore.QSize(32, 32))
        app_icon.addFile(ICON_PATH.format("_48x48"), QtCore.QSize(48, 48))
        app_icon.addFile(ICON_PATH.format(""), QtCore.QSize(64, 64))
        self.setWindowIcon(app_icon)

        # Create little color icons in front of the phase type combo box.
        # Needs to be done pretty much at the beginning because some other
        # stuff relies on the phase type being set.
        pixmap = QtGui.QPixmap(70, 50)
        for phase_type, color in self.seismic_phases.items():
            rgb = matplotlib_color_to_rgb(color)
            pixmap.fill(QtGui.QColor(*rgb))
            icon = QtGui.QIcon(pixmap)
            self.widgets.qComboBox_phaseType.addItem(icon, phase_type)
        rgb = matplotlib_color_to_rgb(self._magnitude_color)
        pixmap.fill(QtGui.QColor(*rgb))
        icon = QtGui.QIcon(pixmap)
        self.widgets.qComboBox_phaseType.addItem(icon, 'Mag')

        self.qMain = self.widgets.centralwidget
        # Add write methods to stdout/stderr text edits in GUI displays to
        # enable redirections for stdout and stderr.
        # we need to remember the original handles because we only write on the
        # console during debug modus.
        self.stdout_backup = sys.stdout
        self.stderr_backup = sys.stderr
        # We automatically redirect all messages to both console and Gui boxes
        sys.stdout = SplitWriter(sys.stdout, self.widgets.qPlainTextEdit_stdout)
        sys.stderr = SplitWriter(sys.stderr, self.widgets.qPlainTextEdit_stderr)
        # set up loggers
        log1 = logging.getLogger("log1")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter('%(message)s'))
        log1.addHandler(sh)
        log2 = logging.getLogger("log2")
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter('%(message)s'))
        log2.addHandler(sh)
        log2.setLevel("DEBUG")
        self.log2 = log2
        self.error = self.log2.error
        # XXX TODO: parse verbose flag from command line
        loglevel = LOGLEVELS.get(config.get("base", "verbosity"), None)
        if loglevel is None:
            loglevel = "CRITICAL"
            self.error("unknown loglevel ('%s'), using loglevel 'normal'." % (
                config.get("base", "verbosity")))
        log1.setLevel(loglevel)
        self.log1 = log1
        self.info = self.log1.info
        self.critical = self.log1.critical
        self.debug = self.log1.debug
        logging.getLogger().handlers = []

        # Matplotlib figure.
        # we bind the figure to the FigureCanvas, so that it will be
        # drawn using the specific backend graphic functions
        self.canv = self.widgets.qMplCanvas
        # We have to reset all splitters such that the widget with the canvas
        # in it can not be collapsed as this leads to a crash of the program
        _i = self.widgets.qSplitter_vertical.indexOf(self.widgets.qSplitter_horizontal)
        self.widgets.qSplitter_vertical.setCollapsible(_i, False)
        _i = self.widgets.qSplitter_horizontal.indexOf(self.widgets.qWidget_mpl)
        self.widgets.qSplitter_horizontal.setCollapsible(_i, False)
        # XXX this resizing operation (buttons minimum size) should be done in
        # XXX the qt_designer.ui but I didn't find the correct settings there..
        self.widgets.qSplitter_horizontal.setSizes([1, 800, 0])
        self.widgets.qSplitter_vertical.setSizes([800, 90])
        # Bind the canvas to the mouse wheel event. Use Qt events for it
        # because the matplotlib events seem to have a problem with Debian.
        self.widgets.qMplCanvas.wheelEvent = self.__mpl_wheelEvent
        #self.keyPressEvent = self.__mpl_keyPressEvent

        # XXX # fetch event data via fdsn, arrivals from taup
        # XXX if self.options.noevents:
        # XXX     fdsn_events, taup_arrivals, msg = None, None, None
        # XXX else:
        # XXX     fdsn_events, taup_arrivals, msg = \
        # XXX         get_event_info(self.T0, self.T1, self.streams)
        # XXX     if fdsn_events is None:
        # XXX         print >> sys.stderr, "Could not determine possible arrivals using obspy.fdsn/taup."
        # XXX     if msg:
        # XXX         self.error(msg)
        # XXX self.taup_arrivals = taup_arrivals
        # XXX if fdsn_events is None:
        # XXX     self.taup_arrivals = []
        # XXX else:
        # XXX     print "%i event(s) with possible arrivals found using obspy.fdsn/taup:" % len(fdsn_events)
        # XXX     for ev in fdsn_events:
        # XXX         o = ev.origins[0]
        # XXX         m = ev.magnitudes[0]
        # XXX         print " ".join([str(o.time), str(m.magnitude_type),
        # XXX                         str(m.mag), str(o.region)])
        self.taup_arrivals = []

        self.fig = self.widgets.qMplCanvas.fig
        facecolor = self.qMain.palette().color(QtGui.QPalette.Window).getRgb()
        self.fig.set_facecolor([value / 255.0 for value in facecolor])

        try:
            self.tmp_dir = setup_external_programs(options, config)
        except IOError:
            msg = "Cannot find external programs dir, localization " + \
                  "methods/functions are deactivated"
            warnings.warn(msg)

        try:
            self.info('Using temporary directory: ' + self.tmp_dir)

            self.catalog = Catalog()
            event = Event()
            event.set_creation_info_username(self.username)
            self.catalog.events = [event]
            # self.setXMLEventID()
            # indicates which of the available focal mechanisms is selected
            self.focMechCurrent = None
            _cmap_name = self._get_config_value(
                "base", "spectrogram_colormap",
                default=mpl.rcParams.get('image.cmap', 'jet'))
            try:
                cmap_spectrogram = getattr(obspy_cm, _cmap_name)
            except AttributeError:
                cmap_spectrogram = get_cmap(_cmap_name)
            self.spectrogramColormap = cmap_spectrogram
            # indicates which of the available events from seishub was loaded
            self.seishubEventCurrent = None
            # indicates how many events are available from seishub
            self.seishubEventCount = None
            # connect to server for event pull/push if not already connected
            event_server_name = config.get("base", "event_server")
            if event_server_name:
                self.event_server = connect_to_server(event_server_name, config,
                                                      clients)
                self.event_server_type = config.get(event_server_name, "type")
            else:
                self.event_server = None
                self.event_server_type = None
            # for transition to Jane, temporarily do both
            test_event_server_name = self._get_config_value(
                "base", "test_event_server_jane", default=None,
                no_option_error_message=False)
            if test_event_server_name:
                self.test_event_server = connect_to_server(
                    test_event_server_name, config, clients)
            else:
                self.test_event_server = None

            # save input raw data and metadata for eventual reuse
            _save_input_data(streams, inventories, self.tmp_dir)

            (warn_msg, merge_msg, streams) = \
                    merge_check_and_cleanup_streams(streams, options, config)

            # if it's not empty show the merge info message now
            if merge_msg:
                self.info(merge_msg)
            # exit if no streams are left after removing everything not suited.
            if not streams:
                err = "No streams left to work with after removing bad streams."
                raise Exception(err)

            # set up dictionaries to store phase_type/axes/line informations
            self.lines = {}
            self.texts = {}

            # sort streams by station name
            streams.sort(key=lambda st: st[0].stats['station'])
            if not config.get("base", "no_metadata"):
                streams = cleanup_streams_without_metadata(streams)
            self.streams_bkp = [st.copy() for st in streams]
            self._setup_4_letter_station_map()
            # XXX TODO replace old 'eventMapColors'

            #Define a pointer to navigate through the streams
            self.stNum = len(streams)
            self.stPt = 0

            if options.event:
                self.setEventFromFilename(options.event)

            self.drawAxes()
            self.multicursor = MultiCursor(self.canv, self.axs, useblit=True,
                                           color='k', linewidth=1, ls='dotted')

            # Initialize the stream related widgets with the right values:
            self.update_stream_name_combobox_from_streams()

            # set the filter/trigger default values according to command line
            # options or optionparser default values
            self.widgets.qDoubleSpinBox_highpass.setValue(
                self.config.getfloat("gui_defaults", "filter_highpass"))
            self.widgets.qDoubleSpinBox_lowpass.setValue(
                self.config.getfloat("gui_defaults", "filter_lowpass"))
            self.widgets.qDoubleSpinBox_corners.setValue(
                self.config.getint("gui_defaults", "filter_corners"))
            self.widgets.qDoubleSpinBox_sta.setValue(
                self.config.getfloat("gui_defaults", "sta"))
            self.widgets.qDoubleSpinBox_lta.setValue(
                self.config.getfloat("gui_defaults", "lta"))
            self.widgets.qToolButton_filter.setChecked(
                self.config.getboolean("gui_defaults", "filter"))
            self.updateStreamLabels()

            self.error(warn_msg)

            # XXX mpl connect XXX XXX XXX XXX XXX
            # XXX http://eli.thegreenplace.net/files/prog_code/qt_mpl_bars.py.txt
            # XXX http://eli.thegreenplace.net/2009/01/20/matplotlib-with-pyqt-guis/
            # XXX https://www.packtpub.com/sites/default/files/sample_chapters/7900-matplotlib-for-python-developers-sample-chapter-6-embedding-matplotlib-in-qt-4.pdf
            # XXX mpl connect XXX XXX XXX XXX XXX
            # Activate all mouse/key/Cursor-events
            # XXX MAYBE rename the event handles again so that they DONT get
            # XXX autoconnected via Qt?!?!?
            self.canv.mpl_connect('key_press_event', self.__mpl_keyPressEvent)
            self.canv.mpl_connect('button_release_event', self.__mpl_mouseButtonReleaseEvent)
            # The scroll event is handled using Qt.
            #self.canv.mpl_connect('scroll_event', self.__mpl_wheelEvent)
            self.canv.mpl_connect('button_press_event', self.__mpl_mouseButtonPressEvent)
            self.canv.mpl_connect('motion_notify_event', self.__mpl_motionNotifyEvent)
            self.multicursorReinit()
            self.canv.show()
            #self.showMaximized()
            self.show()
            # XXX XXX the good old focus issue again!?! no events get to the mpl canvas
            # XXX self.canv.setFocusPolicy(Qt.WheelFocus)
            #print self.canv.hasFocus()

            if self.event_server:
                if not isinstance(self.event_server, SeisHubClient):
                    msg = ("Only SeisHub implemented as event server right now.")
                    raise NotImplementedError(msg)

            if not self.event_server or not isinstance(self.event_server, SeisHubClient):
                msg = ("Warning: SeisHub specific features will not work "
                       "(e.g. 'send Event').")
                self.error(msg)

            if self.event_server:
                self.updateEventListFromSeisHub(self.T0, self.T1)

            self.setFocusToMatplotlib()
        except:
            self.cleanup(skip_duplicate_check=True)
            raise

    def _get_config_value(self, section, key, default=None,
                          no_option_error_message=True, type=str):
        """
        Get a config key value, optionally with default value if key is missing
        showing a warning about it by default.

        :param type: str, int, float or bool
        """
        if type is bool:
            config_getter = self.config.getboolean
        elif type is int:
            config_getter = self.config.getint
        elif type is float:
            config_getter = self.config.getfloat
        elif type is str:
            config_getter = self.config.get
        else:
            raise ValueError()
        try:
            return config_getter(section, key)
        except NoOptionError:
            if no_option_error_message:
                msg = ("No configuration option '{key}' in section "
                       "'{section}'. Defaulting to '{default}'").format(
                           key=key, section=section, default=str(default))
                self.error(msg)
            return default

    def getCurrentStream(self):
        """
        returns currently active/displayed stream
        """
        return self.streams[self.stPt]

    def getCurrentPhase(self):
        """
        returns currently active phase as a string
        """
        return str(self.widgets.qComboBox_phaseType.currentText())

    def time_abs2rel(self, abstime):
        """
        Converts an absolute UTCDateTime to the time in ObsPyck's relative time
        frame.

        :type abstime: :class:`obspy.core.utcdatetime.UTCDateTime`
        :param abstime: Absolute time in UTC.
        :returns: time in ObsPyck's relative time as a float
        """
        return abstime - self.TREF

    def time_rel2abs(self, reltime):
        """
        Converts a relative time in global relative time system to the absolute
        UTCDateTime.

        :type reltime: float
        :param reltime: Relative time in ObsPyck's realtive time frame
        :returns: absolute UTCDateTime
        """
        return self.TREF + reltime

    def cleanup(self, skip_duplicate_check=False):
        """
        Cleanup and prepare for quit.
        Do:
            - check if sysop duplicates are there
            - remove temporary directory and all contents
        """
        if not skip_duplicate_check and self.event_server:
            self.checkForSysopEventDuplicates(self.T0, self.T1)
        try:
            shutil.rmtree(self.tmp_dir)
        except:
            pass

    ###########################################################################
    ### signal handlers START #################################################
    ###########################################################################

    def on_qToolButton_overview_toggled(self):
        state = self.widgets.qToolButton_overview.isChecked()
        widgets_leave_active = ("qToolButton_overview",
                                "qPlainTextEdit_stdout",
                                "qPlainTextEdit_stderr")
        for name in WIDGET_NAMES:
            if name not in widgets_leave_active:
                widget = getattr(self.widgets, name)
                widget.setEnabled(not state)
        if state:
            self.delAxes()
            self.fig.clear()
            self.drawStreamOverview()
            self.multicursor.visible = False
            self.canv.draw()
        else:
            self.delAxes()
            self.fig.clear()
            self.drawAxes()
            self.updateAllItems()
            self.multicursorReinit()
            self.updatePlot()
            self.updateStreamLabels()
            self.canv.draw()

    def on_qToolButton_clearAll_clicked(self, *args):
        # Workaround for overloaded signals:
        #  - "clicked" signal get emitted once without *args and once with an
        #    int as additional argument
        #  - we have to be flexible in the call, otherwise we get errors
        #  - we have to catch one signal, otherwise the action gets performed
        #    twice
        if args:
            return
        self.clearEvent()
        self.updateAllItems()
        self.redraw()

    def on_qToolButton_clearOrigMag_clicked(self, *args):
        if args:
            return
        self.clearOriginMagnitude()
        self.updateAllItems()
        self.redraw()

    def on_qToolButton_clearFocMec_clicked(self, *args):
        if args:
            return
        self.clearFocmec()

    def on_qToolButton_doHyp2000_clicked(self, *args):
        if args:
            return
        #self.delAllItems()
        self.clearOriginMagnitude()
        # self.setXMLEventID()
        self.doHyp2000()
        self.loadHyp2000Data()
        self.calculateEpiHypoDists()
        self.updateMagnitude()
        self.updateAllItems()
        self.redraw()
        self.widgets.qToolButton_showMap.setChecked(True)

    def on_qToolButton_doNlloc_clicked(self, *args):
        if args:
            return
        #self.delAllItems()
        self.clearOriginMagnitude()
        # self.setXMLEventID()
        self.doNLLoc()
        self.loadNLLocOutput()
        self.calculateEpiHypoDists()
        self.updateMagnitude()
        self.updateAllItems()
        self.redraw()
        self.widgets.qToolButton_showMap.setChecked(True)

    def on_qToolButton_doFocMec_clicked(self, *args):
        if args:
            return
        self.clearFocmec()
        self.doFocmec()
        # self.setXMLEventID()

    def on_qToolButton_showMap_toggled(self):
        state = self.widgets.qToolButton_showMap.isChecked()
        widgets_leave_active = ("qToolButton_showMap",
                                "qPlainTextEdit_stdout",
                                "qPlainTextEdit_stderr")
        for name in WIDGET_NAMES:
            if name not in widgets_leave_active:
                widget = getattr(self.widgets, name)
                widget.setEnabled(not state)
        # XXX XXX would be better to avoid list of widget names nd do it
        # XXX XXX dynamically, but it doesnt work..
        # XXX tmp = (getattr(self.widgets, name) for name in widgets_leave_active)
        # XXX for widget in self.children():
        # XXX     print "%s\n" % widget.objectName()
        # XXX     widget.setEnabled(not state)
        # XXX for widget in tmp:
        # XXX     widget.setEnabled(state)
        if state:
            self.delAxes()
            self.fig.clear()
            self.drawEventMap()
            self.multicursor.visible = False
            self.canv.draw()
            #print "http://maps.google.de/maps?f=q&q=%.6f,%.6f" % \
            #       (self.dictOrigin['Latitude'], self.dictOrigin['Longitude'])
        else:
            self.delEventMap()
            self.fig.clear()
            self.drawAxes()
            self.updateAllItems()
            self.multicursorReinit()
            self.updatePlot()
            self.updateStreamLabels()
            self.canv.draw()

    def on_qToolButton_showFocMec_toggled(self):
        state = self.widgets.qToolButton_showFocMec.isChecked()
        widgets_leave_active = ("qToolButton_showFocMec",
                                "qToolButton_nextFocMec",
                                "qPlainTextEdit_stdout",
                                "qPlainTextEdit_stderr")
        for name in WIDGET_NAMES:
            if name not in widgets_leave_active:
                widget = getattr(self.widgets, name)
                widget.setEnabled(not state)
        if state:
            self.delAxes()
            self.fig.clear()
            self.drawFocMec()
            self.multicursor.visible = False
            self.canv.draw()
        else:
            self.delFocMec()
            self.fig.clear()
            self.drawAxes()
            self.updateAllItems()
            self.multicursorReinit()
            self.updatePlot()
            self.updateStreamLabels()
            self.canv.draw()

    def on_qToolButton_nextFocMec_clicked(self, *args):
        if args:
            return
        self.nextFocMec()
        if self.widgets.qToolButton_showFocMec.isChecked():
            self.delFocMec()
            self.fig.clear()
            self.drawFocMec()
            self.canv.draw()

    def on_qToolButton_showWadati_toggled(self):
        state = self.widgets.qToolButton_showWadati.isChecked()
        widgets_leave_active = ("qToolButton_showWadati",
                                "qPlainTextEdit_stdout",
                                "qPlainTextEdit_stderr")
        for name in WIDGET_NAMES:
            if name not in widgets_leave_active:
                widget = getattr(self.widgets, name)
                widget.setEnabled(not state)
        if state:
            self.delAxes()
            self.fig.clear()
            self.drawWadati()
            self.multicursor.visible = False
            self.canv.draw()
        else:
            self.delWadati()
            self.fig.clear()
            self.drawAxes()
            self.updateAllItems()
            self.multicursorReinit()
            self.updateCurrentStream()
            self.updatePlot()
            self.updateStreamLabels()
            self.canv.draw()

    def on_qToolButton_getNextEvent_clicked(self, *args):
        if args:
            return
        # check if event list is empty and force an update if this is the case
        if not hasattr(self, "seishubEventList"):
            self.updateEventListFromSeisHub(self.T0, self.T1)
        if not self.seishubEventList:
            self.critical("No events available from SeisHub.")
            return
        # iterate event number to fetch
        self.seishubEventCurrent = (self.seishubEventCurrent + 1) % \
                                   self.seishubEventCount
        event = self.seishubEventList[self.seishubEventCurrent]
        resource_name = str(event.get('resource_name'))
        self.clearEvent()
        self.getEventFromSeisHub(resource_name)
        self.updateAllItems()
        self.redraw()

    def on_qToolButton_updateEventList_clicked(self, *args):
        if args:
            return
        self.updateEventListFromSeisHub(self.T0, self.T1)

    def on_qToolButton_sendNewEvent_clicked(self, *args):
        if args:
            return
        # if sysop event and information missing show error and abort upload
        if self.widgets.qCheckBox_public.isChecked():
            if not self.widgets.qCheckBox_sysop.isChecked():
                err = "Error: Enter password for \"sysop\"-account first."
                self.error(err)
                return
            ok, msg = self.checkForCompleteEvent()
            if not ok:
                self.popupBadEventError(msg)
                return
        self.upload_event()
        self.on_qToolButton_updateEventList_clicked()
        self.checkForSysopEventDuplicates(self.T0, self.T1)

    def on_qToolButton_replaceEvent_clicked(self, *args):
        if args:
            return
        # if sysop event and information missing show error and abort upload
        if self.widgets.qCheckBox_public.isChecked():
            if not self.widgets.qCheckBox_sysop.isChecked():
                err = "Error: Enter password for \"sysop\"-account first."
                self.error(err)
                return
            ok, msg = self.checkForCompleteEvent()
            if not ok:
                self.popupBadEventError(msg)
                return
        event = self.seishubEventList[self.seishubEventCurrent]
        resource_name = event.get('resource_name')
        if not resource_name.startswith("obspyck_"):
            err = "Error: Only replacing of events created with ObsPyck allowed."
            self.error(err)
            return
        event_id = resource_name.split("_")[1]
        try:
            user = event.creation_info.author
        except:
            user = None
        qMessageBox = QtWidgets.QMessageBox()
        app_icon = QtGui.QIcon()
        app_icon.addFile(ICON_PATH.format("_16x16"), QtCore.QSize(16, 16))
        app_icon.addFile(ICON_PATH.format("_24x42"), QtCore.QSize(24, 24))
        app_icon.addFile(ICON_PATH.format("_32x32"), QtCore.QSize(32, 32))
        app_icon.addFile(ICON_PATH.format("_48x48"), QtCore.QSize(48, 48))
        app_icon.addFile(ICON_PATH.format(""), QtCore.QSize(64, 64))
        qMessageBox.setWindowIcon(app_icon)
        qMessageBox.setIcon(QtWidgets.QMessageBox.Warning)
        qMessageBox.setWindowTitle("Replace?")
        qMessageBox.setText("Overwrite event in database?")
        msg = "%s  (user: %s)" % (resource_name, user)
        msg += "\n\nWarning: Loading and then sending events might result " + \
               "in loss of information in the xml file (e.g. all custom " + \
               "defined fields!)"
        qMessageBox.setInformativeText(msg)
        qMessageBox.setStandardButtons(QtWidgets.QMessageBox.Cancel | QtWidgets.QMessageBox.Ok)
        qMessageBox.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        if qMessageBox.exec_() == QtWidgets.QMessageBox.Ok:
            self.delete_event(resource_name)
            self.setXMLEventID(event_id)
            self.upload_event()
            self.on_qToolButton_updateEventList_clicked()
            self.checkForSysopEventDuplicates(self.T0, self.T1)

    def on_qToolButton_deleteEvent_clicked(self, *args):
        if args:
            return
        # if sysop event and information missing show error and abort upload
        if self.widgets.qCheckBox_public.isChecked():
            if not self.widgets.qCheckBox_sysop.isChecked():
                err = "Error: Enter password for \"sysop\"-account first."
                self.error(err)
                return
        event = self.seishubEventList[self.seishubEventCurrent]
        resource_name = event.get('resource_name')
        try:
            user = event.creation_info.author
        except:
            user = None
        qMessageBox = QtWidgets.QMessageBox()
        app_icon = QtGui.QIcon()
        app_icon.addFile(ICON_PATH.format("_16x16"), QtCore.QSize(16, 16))
        app_icon.addFile(ICON_PATH.format("_24x42"), QtCore.QSize(24, 24))
        app_icon.addFile(ICON_PATH.format("_32x32"), QtCore.QSize(32, 32))
        app_icon.addFile(ICON_PATH.format("_48x48"), QtCore.QSize(48, 48))
        app_icon.addFile(ICON_PATH.format(""), QtCore.QSize(64, 64))
        qMessageBox.setWindowIcon(app_icon)
        qMessageBox.setIcon(QtWidgets.QMessageBox.Warning)
        qMessageBox.setWindowTitle("Delete?")
        qMessageBox.setText("Delete event from database?")
        msg = "%s  (user: %s)" % (resource_name, user)
        qMessageBox.setInformativeText(msg)
        qMessageBox.setStandardButtons(QtWidgets.QMessageBox.Cancel | QtWidgets.QMessageBox.Ok)
        qMessageBox.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        if qMessageBox.exec_() == QtWidgets.QMessageBox.Ok:
            self.delete_event(resource_name)
            self.on_qToolButton_updateEventList_clicked()

    def on_qToolButton_saveEventLocally_clicked(self, *args):
        if args:
            return
        self.save_event_locally()

    def on_qCheckBox_sysop_toggled(self):
        self.on_qLineEdit_sysopPassword_editingFinished()
        newstate = self.widgets.qCheckBox_sysop.isChecked()
        if not str(self.widgets.qLineEdit_sysopPassword.text()):
            self.widgets.qCheckBox_sysop.setChecked(False)
            err = "Error: Enter password for \"sysop\"-account first."
            self.error(err)
        else:
            self.info("Setting usage of \"sysop\"-account to: %s" % newstate)

    # the corresponding signal is emitted when hitting return after entering
    # the password
    def on_qLineEdit_sysopPassword_editingFinished(self):
        if not self.event_server:
            self.widgets.qCheckBox_sysop.setChecked(False)
            self.widgets.qLineEdit_sysopPassword.clear()
            err = "Error: No event server specified"
            self.error(err)
            return
        event_server = self.config.get("base", "event_server")
        passwd = str(self.widgets.qLineEdit_sysopPassword.text())
        tmp_client = SeisHubClient(
            base_url=self.config.get(event_server, "base_url"),
            user="sysop", password=passwd)
        if tmp_client.test_auth():
            self.clients['__SeisHub-sysop__'] = tmp_client
            self.widgets.qCheckBox_sysop.setChecked(True)
        # if authentication test fails empty password field and uncheck sysop
        else:
            self.clients.pop('__SeisHub-sysop__', None)
            self.widgets.qCheckBox_sysop.setChecked(False)
            self.widgets.qLineEdit_sysopPassword.clear()
            err = "Error: Authentication as sysop failed! (Wrong password!?)"
            self.error(err)
        self.canv.setFocus() # XXX needed??
    # XXX XXX not used atm. relict from gtk when buttons snatch to grab the
    # XXX XXX focus away from the mpl-canvas to which key/mouseButtonPresses are
    # XXX XXX connected
    # XXX def on_buttonSetFocusOnPlot_clicked(self, event):
    # XXX     self.setFocusToMatplotlib()

    def on_qToolButton_debug_clicked(self, *args):
        if args:
            return
        self.debugger()

    def on_qToolButton_sort_abc_clicked(self, *args):
        if args:
            return
        self.streams_bkp.sort(key=lambda stream: stream[0].id)
        self.streams = [st.copy() for st in self.streams_bkp]
        self.stPt = 0
        self.widgets.qComboBox_streamName.setCurrentIndex(self.stPt)
        self.update_stream_name_combobox_from_streams()

    def on_qToolButton_sort_distance_clicked(self, *args):
        if args:
            return
        self.streams_bkp.sort(key=self.epidist_for_stream)
        self.streams = [st.copy() for st in self.streams_bkp]
        epidists = [self.epidist_for_stream(st) for st in self.streams_bkp]
        suffixes = ['{:.1f}km'.format(dist) if dist is not None else '??km'
                    for dist in epidists]
        self.stPt = 0
        self.widgets.qComboBox_streamName.setCurrentIndex(self.stPt)
        self.update_stream_name_combobox_from_streams(suffixes=suffixes)

    def on_qToolButton_previousStream_clicked(self, *args):
        if args:
            return
        self.stPt = (self.stPt - 1) % self.stNum
        self.widgets.qComboBox_streamName.setCurrentIndex(self.stPt)

    def on_qComboBox_streamName_currentIndexChanged(self, newvalue):
        # signal gets emitted twice, once with the index of the new field,
        # once with it's value
        if not isinstance(newvalue, int):
            return
        self.stPt = self.widgets.qComboBox_streamName.currentIndex()
        self.streams[self.stPt] = self.streams_bkp[self.stPt].copy()
        stats = self.streams[self.stPt][0].stats
        self.info("Going to stream: %s.%s" % (stats.network, stats.station))
        self.drawStream()
        self.updateStreamNumberLabel()

    def on_qToolButton_nextStream_clicked(self, *args):
        if args:
            return
        self.stPt = (self.stPt + 1) % self.stNum
        self.widgets.qComboBox_streamName.setCurrentIndex(self.stPt)

    def on_qComboBox_phaseType_currentIndexChanged(self, newvalue):
        # XXX: Ugly hack because it can be called before the combo box has any
        # entries.
        try:
            self.updateMulticursorColor()
            self.redraw()
        except AttributeError:
            pass

    def on_qToolButton_physical_units_toggled(self):
        self.streams[self.stPt] = self.streams_bkp[self.stPt].copy()
        self.drawStream()

    def on_qDoubleSpinBox_waterlevel_valueChanged(self, newvalue):
        if not self.widgets.qToolButton_physical_units.isChecked():
            self.canv.setFocus() # XXX needed??
            return
        self.updateCurrentStream()
        self.updatePlot()
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed?? # XXX do we still need this focus grabbing with QT??? XXX XXX XXX XXX

    def on_qToolButton_filter_toggled(self):
        self.updateCurrentStream()
        self.updatePlot()

    def on_qToolButton_rotateLQT_toggled(self):
        if self.widgets.qToolButton_rotateLQT.isChecked():
            self.widgets.qToolButton_rotateZRT.setChecked(False)
        self.drawStream()

    def on_qToolButton_rotateZRT_toggled(self):
        if self.widgets.qToolButton_rotateZRT.isChecked():
            self.widgets.qToolButton_rotateLQT.setChecked(False)
        self.drawStream()

    def on_qToolButton_trigger_toggled(self):
        self.drawStream()

    def on_qToolButton_arpicker_clicked(self, *args):
        """
        Set automatic P/S picks using the AR picker.
        """
        if args:
            return
        self.clearEvent()
        self.updateAllItems()
        self._arpicker()
        self.updateAllItems()
        self.redraw()

    def on_qComboBox_filterType_currentIndexChanged(self, newvalue):
        if not self.widgets.qToolButton_filter.isChecked():
            return
        self.updateCurrentStream()
        self.updatePlot()

    def on_qCheckBox_zerophase_toggled(self):
        if not self.widgets.qToolButton_filter.isChecked():
            return
        self.updateCurrentStream()
        self.updatePlot()

    def on_qCheckBox_50Hz_toggled(self):
        if not self.widgets.qToolButton_filter.isChecked():
            return
        self.updateCurrentStream()
        self.updatePlot()

    def on_qDoubleSpinBox_highpass_valueChanged(self, newvalue):
        widgets = self.widgets
        stats = self.streams[self.stPt][0].stats
        if not widgets.qToolButton_filter.isChecked() or \
                str(widgets.qComboBox_filterType.currentText()) == "Lowpass":
            self.canv.setFocus() # XXX needed??
            return
        # if the filter flag is not set, we don't have to update the plot
        # XXX if we have a lowpass, we dont need to update!! Not yet implemented!! XXX
        if widgets.qDoubleSpinBox_lowpass.value() < newvalue:
            err = "Warning: Lowpass frequency below Highpass frequency!"
            self.error(err)
        # XXX maybe the following check could be done nicer
        # XXX check this criterion!
        minimum  = float(stats.sampling_rate) / stats.npts
        if newvalue < minimum:
            err = "Warning: Lowpass frequency is not supported by length of trace!"
            self.error(err)
        self.updateCurrentStream()
        self.updatePlot()
        # XXX we could use this for the combobox too!
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed?? # XXX do we still need this focus grabbing with QT??? XXX XXX XXX XXX

    def on_qDoubleSpinBox_corners_valueChanged(self, newvalue):
        widgets = self.widgets
        if not widgets.qToolButton_filter.isChecked():
            self.canv.setFocus() # XXX needed??
            return
        # if the filter flag is not set, we don't have to update the plot
        # XXX if we have a lowpass, we dont need to update!! Not yet implemented!! XXX
        self.updateCurrentStream()
        self.updatePlot()
        # XXX we could use this for the combobox too!
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed?? # XXX do we still need this focus grabbing with QT??? XXX XXX XXX XXX

    def on_qDoubleSpinBox_lowpass_valueChanged(self, newvalue):
        widgets = self.widgets
        stats = self.streams[self.stPt][0].stats
        if not widgets.qToolButton_filter.isChecked() or \
           str(widgets.qComboBox_filterType.currentText()) == "Highpass":
            self.canv.setFocus() # XXX needed??
            return
        # if the filter flag is not set, we don't have to update the plot
        # XXX if we have a highpass, we dont need to update!! Not yet implemented!! XXX
        if newvalue < widgets.qDoubleSpinBox_highpass.value():
            err = "Warning: Lowpass frequency below Highpass frequency!"
            self.error(err)
        # XXX maybe the following check could be done nicer
        # XXX check this criterion!
        maximum  = stats.sampling_rate / 2.0 # Nyquist
        if newvalue > maximum:
            err = "Warning: Highpass frequency is lower than Nyquist!"
            self.error(err)
        self.updateCurrentStream()
        self.updatePlot()
        # XXX we could use this for the combobox too!
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed??

    def on_qDoubleSpinBox_sta_valueChanged(self, newvalue):
        widgets = self.widgets
        # if the trigger flag is not set, we don't have to update the plot
        if not widgets.qToolButton_trigger.isChecked():
            self.canv.setFocus() # XXX needed??
            return
        self.updateCurrentStream()
        self.updatePlot()
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed?? # XXX do we still need this focus grabbing with QT??? XXX XXX XXX XXX

    def on_qDoubleSpinBox_lta_valueChanged(self, newvalue):
        widgets = self.widgets
        # if the trigger flag is not set, we don't have to update the plot
        if not widgets.qToolButton_trigger.isChecked():
            self.canv.setFocus() # XXX needed??
            return
        self.updateCurrentStream()
        self.updatePlot()
        # reset focus to matplotlib figure
        self.canv.setFocus() # XXX needed?? # XXX do we still need this focus grabbing with QT??? XXX XXX XXX XXX

    def on_qToolButton_spectrogram_toggled(self):
        state = self.widgets.qToolButton_spectrogram.isChecked()
        widgets_deactivate = ("qToolButton_physical_units", "qDoubleSpinBox_waterlevel", "qToolButton_filter", "qToolButton_overview",
                "qComboBox_filterType", "qCheckBox_zerophase",
                "qCheckBox_50Hz", "qDoubleSpinBox_corners",
                "qLabel_highpass", "qLabel_lowpass", "qDoubleSpinBox_highpass",
                "qDoubleSpinBox_lowpass", "qToolButton_rotateLQT",
                "qToolButton_rotateZRT", "qToolButton_trigger")
        for name in widgets_deactivate:
            widget = getattr(self.widgets, name)
            widget.setEnabled(not state)
        if state:
            msg = "Showing spectrograms (takes a few seconds with log-option)."
        else:
            msg = "Showing seismograms."
        self.info(msg)
        xmin, xmax = self.axs[0].get_xlim()
        #self.delAllItems()
        self.delAxes()
        self.fig.clear()
        print('Drawing new axes')
        self.drawAxes()
        print('Updating items')
        self.updateAllItems()
        self.multicursorReinit()
        self.axs[0].set_xlim(xmin, xmax)
        print('Updating stream')
        self.updateCurrentStream()
        self.updatePlot()

    def on_qCheckBox_spectrogramLog_toggled(self):
        if self.widgets.qToolButton_spectrogram.isChecked():
            self.on_qToolButton_spectrogram_toggled()

    def on_qDoubleSpinBox_wlen_valueChanged(self):
        if self.widgets.qToolButton_spectrogram.isChecked():
            self.on_qToolButton_spectrogram_toggled()

    def on_qDoubleSpinBox_perlap_valueChanged(self):
        if self.widgets.qToolButton_spectrogram.isChecked():
            self.on_qToolButton_spectrogram_toggled()

    def on_qPushButton_qml_update_clicked(self):
        self.update_qml_text()

    ###########################################################################
    ### signal handlers END ###### ############################################
    ###########################################################################

    def update_qml_text(self, qml=None):
        if qml is None:
            qml = self.get_QUAKEML_string()
        self.widgets.qTextEdit_qml.setText(qml)

    def _filter(self, stream):
        """
        Applies filter currently selected in GUI to Trace or Stream object.
        Also displays a message.
        """
        # get taper settings from config
        try:
            taper_max_length = self._get_config_value(
                'base', 'taper_max_length', default=5, type=float)
        except Exception as e:
            print(e)
        try:
            taper_max_percentage = self._get_config_value(
                'base', 'taper_max_percentage', default=0.05, type=float)
        except Exception as e:
            print(e)
        try:
            taper_type = self._get_config_value(
                'base', 'taper_type', default='cosine', type=str)
        except Exception as e:
            print(e)
        w = self.widgets
        type = str(w.qComboBox_filterType.currentText()).lower()
        options = {}
        options['corners'] = int(w.qDoubleSpinBox_corners.value())
        options['zerophase'] = w.qCheckBox_zerophase.isChecked()
        msg = ""
        if type in ("bandpass", "bandstop"):
            options['freqmin'] = w.qDoubleSpinBox_highpass.value()
            options['freqmax'] = w.qDoubleSpinBox_lowpass.value()
        elif type == "lowpass":
            options['freq'] = w.qDoubleSpinBox_lowpass.value()
        elif type == "highpass":
            options['freq'] = w.qDoubleSpinBox_highpass.value()
        if type in ("bandpass", "bandstop"):
            msg = "%s (zerophase=%s): %.2f-%.2f Hz" % \
                    (type, options['zerophase'],
                     options['freqmin'], options['freqmax'])
        elif type in ("lowpass", "highpass"):
            msg = "%s (zerophase=%s): %.2f Hz" % \
                    (type, options['zerophase'], options['freq'])
        try:
            stream.detrend("linear")
            try:
                stream.taper(max_percentage=taper_max_percentage,
                             max_length=taper_max_length, type=taper_type)
            except:
                stream.taper()
                msg = ('Error in stream tapering (old obspy version?). '
                       'Tapering will be performed with Trace.taper() '
                       'defaults.')
                self.error(msg)
            if w.qCheckBox_50Hz.isChecked():
                for i_ in range(2):
                    stream.filter("bandstop", freqmin=46, freqmax=54,
                                  corners=2, zerophase=options['zerophase'])
                msg2 = "50Hz Bandstop"
                self.info(msg2)
            stream.filter(type, **options)
            self.info(msg)
        except:
            err = "Error during filtering. Showing unfiltered data."
            self.error(err)

    def _physical_units(self, stream):
        """
        Corrects to physical units (m/s or m or m/s**2), as specified by
        configuration file.
        """
        if isinstance(stream, Trace):
            stream = Stream(traces=[stream])

        w = self.widgets
        water_level = float(w.qDoubleSpinBox_waterlevel.value())
        output_units = self._get_config_value('base', 'physical_units',
                                              default='velocity')
        if output_units == 'velocity':
            output_units = 'VEL'
            label = '[m/s]'
        elif output_units == 'displacement':
            output_units = 'DISP'
            label = '[m]'
        elif output_units == 'acceleration':
            output_units = 'ACC'
            label = '[m/s**2]'
        else:
            msg = ('Unrecognized physical units in configuration option '
                   '"physical_units" in section "base": "{}". Defaulting '
                   'to m/s for physical units switch.').format(output_units)
            self.error(msg)
            output_units = 'VEL'
            label = '[m/s]'
        self.widgets.qToolButton_physical_units.setText(label)
        msg = "Correcting to {} (water_level={:.1f}).".format(label,
                                                              water_level)

        try:
            if 'parser' in stream[0].stats:
                # metadata from SEED
                for tr in stream:
                    tr.simulate(seedresp={'filename': tr.stats.parser,
                                          'units': output_units},
                                remove_sensitivity=True,
                                water_level=water_level)
            elif 'response' in stream[0].stats:
                # metadata from StationXML
                stream.remove_response(output=output_units,
                                       water_level=water_level)
            else:
                msg = ('No Response object attached to trace, '
                       'can not convert to physical units:\n')
                self.error(msg + str(stream[0].stats))
            self.info(msg)
        except Exception as e:
            err = ("Error during instrument correction. Showing uncorrected "
                   "data.\n" + str(e))
            self.error(err)

    def _rotateLQT(self, stream, origin):
        """
        Rotates stream to LQT with respect to station location in first trace
        of stream and origin information.
        Exception handling should be done outside this function.
        Also displays a message.
        """
        # calculate backazimuth and incidence from station/event geometry
        azim, bazim, inci = coords2azbazinc(stream, origin)
        # replace ZNE data with rotated data
        z = stream.select(component="Z")[0].data
        n = stream.select(component="N")[0].data
        e = stream.select(component="E")[0].data
        self.info("using baz, takeoff: %s, %s" % (bazim, inci))
        l, q, t = rotate_zne_lqt(z, n, e, bazim, inci)
        for comp, data in zip("ZNE", (l, q, t)):
            tr = stream.select(component=comp)[0]
            tr.data = data
            tr.stats.channel = map_rotated_channel_code(tr.stats.channel,
                                                        "LQT")
        self.info("Showing traces rotated to LQT.")

    def _rotateZRT(self, stream, origin):
        """
        Rotates stream to ZRT with respect to station location in first trace
        of stream and origin information.
        Exception handling should be done outside this function.
        Also displays a message.
        """
        # calculate backazimuth from station/event geometry
        azim, bazim, inci = coords2azbazinc(stream, origin)
        # replace NE data with rotated data
        n = stream.select(component="N")[0].data
        e = stream.select(component="E")[0].data
        self.info("using baz: %s" % bazim)
        r, t = rotate_ne_rt(n, e, bazim)
        stream.select(component="N")[0].data = r
        stream.select(component="E")[0].data = t
        for comp, data in zip("NE", (r, t)):
            tr = stream.select(component=comp)[0]
            tr.data = data
            tr.stats.channel = map_rotated_channel_code(tr.stats.channel,
                                                        "ZRT")
        self.info("Showing traces rotated to ZRT.")

    def _trigger(self, stream):
        """
        Run recSTALTA trigger on stream/trace.
        Exception handling should be done outside this function.
        Also displays a message.
        """
        sta = self.widgets.qDoubleSpinBox_sta.value()
        lta = self.widgets.qDoubleSpinBox_lta.value()
        stream.trigger("recstalta", sta=sta, lta=lta)
        self.info("Showing recSTALTA triggered traces.")

    def _arpicker(self):
        """
        Run AR picker on all streams and set P/S picks accordingly.
        Also displays a message.
        """
        try:
            f1 = self.config.getfloat("ar_picker", "f1")
            f2 = self.config.getfloat("ar_picker", "f2")
            sta_p = self.config.getfloat("ar_picker", "sta_p")
            lta_p = self.config.getfloat("ar_picker", "lta_p")
            sta_s = self.config.getfloat("ar_picker", "sta_s")
            lta_s = self.config.getfloat("ar_picker", "lta_s")
            m_p = self.config.getint("ar_picker", "m_p")
            m_s = self.config.getint("ar_picker", "m_s")
            l_p = self.config.getfloat("ar_picker", "l_p")
            l_s = self.config.getfloat("ar_picker", "l_s")
        except (NoOptionError, NoSectionError) as e:
            msg = ('To use AR Picker, you need to have a section [ar_picker] '
                   'in your .obspyckrc with the following keys set: "f1", '
                   '"f2", "lta_p", "sta_p", "lta_s", "sta_s", "m_p", "m_s", '
                   '"l_p", "l_s" (compare documentation for '
                   'obspy.signal.trigger.ar_pick\n%s') % str(e)
            self.error(msg)
            return
        self.info("Setting automatic picks using AR picker:")
        for i, st in enumerate(self.streams):
            try:
                z = st.select(component="Z")[0]
                n = st.select(component="N")[0]
                e = st.select(component="E")[0]
            except IndexError:
                msg = ('AR picker currently only implemented for Z/N/E data, '
                       'but provided stream was:\n%s') % st
                self.error(msg)
                continue
            try:
                assert z.stats.sampling_rate == n.stats.sampling_rate == \
                    e.stats.sampling_rate
            except AssertionError:
                msg = ('AR picker needs same sampling rate on all traces '
                       'but provided stream was:\n%s') % st
                self.error(msg)
                continue
            spr = z.stats.sampling_rate
            p, s = ar_pick(z.data, n.data, e.data, spr, f1, f2, lta_p, sta_p,
                           lta_s, sta_s, m_p, m_s, l_p, l_s)
            for t, phase_hint, tr in zip((p, s), 'PS', (z, n)):
                pick = self.getPick(phase_hint=phase_hint, setdefault=True,
                                    seed_string=tr.id)
                pick.setTime(z.stats.starttime + t)
                self.info(str(pick))
                self.info("%s pick set at %.3f (%s)" % (
                    phase_hint, self.time_abs2rel(pick.time),
                    pick.time.isoformat()))
        self.updateAllItems()
        self.redraw()
        return

    def debugger(self):
        sys.stdout = self.stdout_backup
        sys.stderr = self.stderr_backup
        ## DEBUG PYQT START
        QtCore.pyqtRemoveInputHook()
        import pdb
        pdb.set_trace()
        QtCore.pyqtRestoreInputHook()
        ## DEBUG PYQT END
        self.stdout_backup = sys.stdout
        self.stderr_backup = sys.stderr
        sys.stdout = SplitWriter(sys.stdout, self.widgets.qPlainTextEdit_stdout)
        sys.stderr = SplitWriter(sys.stderr, self.widgets.qPlainTextEdit_stderr)

    def setFocusToMatplotlib(self):
        """
        Sometimes needed to restore Qt focus to matplotlib canvas.
        Otherwise key/mouse events do not end up in our signal handling
        routine.
        """
        self.canv.setFocus()

    def drawPickLabel(self, ax, pick, main_axes=True):
        """
        Draws Labels at pick axvlines.
        """
        # XXX TODO check handling of custom int weights
        if "extra" in pick:
            weight = pick.extra.get("weight", {"value": "_"})
            weight = str(weight["value"])
        else:
            weight = "_"
        label = '%s (%s) %s%s%s' % (
            pick.get("phase_hint", "_"), pick.waveform_id.channel_code,
            ONSET_CHARS.get(pick.onset, "?"),
            POLARITY_CHARS.get(pick.polarity, "?"), weight)
        x = self.time_abs2rel(pick.time)
        if main_axes:
            y = 0.96 - 0.01 * len(self.axs)
            va = "top"
            bbox_fc = "white"
        else:
            y = 0.04 + 0.01 * len(self.axs)
            va = "bottom"
            bbox_fc = "lightgray"
        i = self.axs.index(ax)
        color = self.seismic_phases[pick.phase_hint]
        bbox = dict(boxstyle="round,pad=0.4", fc=bbox_fc, ec="k", lw=1, alpha=1.0)
        ax.text(x, y, label, transform=self.trans[i], color=color,
                family='monospace', va=va, bbox=bbox, size="large",
                zorder=5000, ha="right")

    def drawArrivalLabel(self, ax, arrival, pick):
        """
        Draw the label for an arrival.
        """
        label = '  %s %+.3fs' % (arrival.phase, arrival.time_residual)
        x = self.time_abs2rel(pick.time) + arrival.time_residual
        y = 1 - 0.03 * len(self.axs)
        i = self.axs.index(ax)
        ax.text(x, y, label, transform=self.trans[i], color='k',
                family='monospace', va="top", ha="right")

    def drawIds(self):
        """
        draws the trace ids plotted as text into each axes.
        """
        # make a Stream with the traces that are plotted
        x = 0.01
        y = 0.92
        bbox = dict(boxstyle="round,pad=0.4", fc="w", ec="k", lw=1.5, alpha=1.0)
        kwargs = dict(va="top", ha="left", fontsize=18, family='monospace',
                      zorder=10000)
        if self.widgets.qToolButton_overview.isChecked():
            kwargs['fontsize'] = 10
            for ax, st in zip(self.axs, self.streams):
                offset = len(st[0].id[:-1])
                ax.text(x, y, st[0].id[:-1] + "_" * len(st), color="k",
                        transform=ax.transAxes, bbox=bbox, **kwargs)
                for i_, tr in enumerate(st):
                    color = COMPONENT_COLORS.get(tr.id[-1], "gray")
                    cha = tr.stats.channel
                    if not cha:
                        cha = "???"
                    label = " " * offset + cha[-1]
                    offset = len(label)
                    ax.text(x, y, label, color=color, transform=ax.transAxes,
                            **kwargs)
        else:
            for ax, tr in zip(self.axs, self.streams[self.stPt]):
                ax.text(x, y, tr.id, color="k", transform=ax.transAxes,
                        bbox=bbox, **kwargs)

    def updateIds(self, textcolor):
        """
        updates the trace ids plotted as text into each axes.
        if "rotate" button is on map the component key to LQT or ZRT.
        CAUTION: The last letter of the ID plotted here is used to identify
                 the component when setting S polarities!!
                 Change with caution!!
        """
        # make a Stream with the traces that are plotted
        # if in overview mode this is not one of the original streams but a
        # stream with all the Z traces of all streams
        if self.widgets.qToolButton_overview.isChecked():
            tmp_stream = Stream([st.select(component="Z")[0] for st in self.streams])
        else:
            tmp_stream = self.streams[self.stPt]
        for ax, tr in zip(self.axs, tmp_stream):
            # trace ids are first text-plot so its at position 0
            t = ax.texts[0]
            t.set_text(tr.id)
            t.set_color(textcolor)

    def drawStream(self):
        """
        Calls all subroutines to draw the normal stream view.
        """
        xmin, xmax = self.axs[0].get_xlim()
        #self.delAllItems()
        self.delAxes()
        self.fig.clear()
        self.drawAxes()
        self.updateCurrentStream()
        self.updateAllItems()
        self.multicursorReinit()
        self.axs[0].set_xlim(xmin, xmax)
        self.updatePlot()
        ymax = max([max(abs(p.get_ydata())) for p in self.plts])
        if self.widgets.qToolButton_trigger.isChecked():
            ymin = 0
        else:
            ymin = -ymax
        for ax in self.axs:
            ax.set_ybound(upper=ymax, lower=ymin)
        self.redraw()

    def drawAxes(self):
        st = self.getCurrentStream()
        fig = self.fig
        axs = []
        self.axs = axs
        plts = []
        self.plts = plts
        trans = []
        self.trans = trans
        t = []
        self.t = t
        for i, tr in enumerate(st):
            if i == 0:
                ax = fig.add_subplot(len(st), 1, 1)
            else:
                ax = fig.add_subplot(len(st), 1, i+1, sharex=axs[0], sharey=axs[0])
                ax.xaxis.set_ticks_position("top")
            axs.append(ax)
            # relative x-axis times start with 0 at global reference time
            starttime_relative = self.time_abs2rel(tr.stats.starttime)
            sampletimes = np.arange(starttime_relative,
                    starttime_relative + (tr.stats.delta * tr.stats.npts),
                    tr.stats.delta)
            # XXX sometimes our arange is one item too long (why??), so we just cut
            # off the last item if this is the case
            if len(sampletimes) == tr.stats.npts + 1:
                sampletimes = sampletimes[:-1]
            t.append(sampletimes)
            trans.append(mpl.transforms.blended_transform_factory(ax.transData,
                                                                  ax.transAxes))
            ax.xaxis.set_major_formatter(FuncFormatter(formatXTicklabels))
            if self.widgets.qToolButton_spectrogram.isChecked():
                log = self.widgets.qCheckBox_spectrogramLog.isChecked()
                wlen = self.widgets.qDoubleSpinBox_wlen.value()
                perlap = self.widgets.qDoubleSpinBox_perlap.value()
                print('Trying spect {}.{}'.format(tr.stats.station, tr.stats.channel))
                spectrogram(tr.data, tr.stats.sampling_rate, log=log, wlen=wlen, per_lap=perlap,
                            cmap=self.spectrogramColormap, axes=ax, zorder=-10)
                # adjust spectrogram start time offset, relative to reference time
                if log:
                    quadmesh_ = ax.collections[0]
                    quadmesh_._coordinates[:, :, 0] += self.options.starttime_offset
                else:
                    x1, x2, y1, y2 = ax.images[0].get_extent()
                    ax.images[0].set_extent((
                        x1 + self.options.starttime_offset,
                        x2 + self.options.starttime_offset, y1, y2))
            else:
                # normalize with overall sensitivity and convert to nm/s
                # if not explicitly deactivated on command line
                if self.config.getboolean("base", "normalization") and not self.config.getboolean("base", "no_metadata"):
                    try:
                        sensitivity = tr.stats.parser.get_paz(tr.id, tr.stats.starttime)['sensitivity']
                    except AttributeError:
                        sensitivity = tr.stats.response.instrument_sensitivity.value
                    plts.append(ax.plot(sampletimes, tr.data / sensitivity * 1e9, color='k', zorder=1000)[0])
                else:
                    plts.append(ax.plot(sampletimes, tr.data, color='k', zorder=1000)[0])
        print('Out of trace loop')
        self.drawIds()
        axs[-1].xaxis.set_ticks_position("both")
        label = self.TREF.isoformat().replace("T", "  ")
        self.supTit = fig.suptitle(label, ha="left", va="bottom",
                                   x=0.01, y=0.01)
        self.xMin, self.xMax = axs[0].get_xlim()
        self.yMin, self.yMax = axs[0].get_ylim()
        fig.subplots_adjust(bottom=0.001, hspace=0.000, right=0.999, top=0.999, left=0.001)

    def delAxes(self):
        for ax in self.axs:
            if ax in self.fig.axes:
                self.fig.delaxes(ax)
            del ax
        if self.supTit in self.fig.texts:
            self.fig.texts.remove(self.supTit)

    def redraw(self):
        for line in self.multicursor.lines:
            line.set_visible(False)
        self.canv.draw()

    def updateCurrentStream(self):
        """
        Update current stream either with raw/rotated/filtered data
        according to current button settings in GUI.
        """
        # XXX copying is only necessary if "Filter" or "Rotate" is selected
        # XXX it is simpler for the code to just copy in any case..
        self.streams[self.stPt] = self.streams_bkp[self.stPt].copy()
        st = self.streams[self.stPt]
        # To display filtered data we overwrite our alias to current stream
        # and replace it with the filtered data.
        if self.widgets.qToolButton_physical_units.isChecked():
            self._physical_units(st)
        if self.widgets.qToolButton_filter.isChecked():
            self._filter(st)
        else:
            self.info("Unfiltered Traces.")
        # check if rotation should be performed
        if self.widgets.qToolButton_rotateLQT.isChecked():
            try:
                assert(len(self.catalog[0].origins) > 0), "No origin data"
                origin = self.catalog[0].origins[0]
                self._rotateLQT(st, origin)
            except Exception as e:
                self.widgets.qToolButton_rotateLQT.setChecked(False)
                err = str(e)
                err += "\nError during rotating to LQT. Showing unrotated data."
                self.error(err)
        elif self.widgets.qToolButton_rotateZRT.isChecked():
            try:
                assert(len(self.catalog[0].origins) > 0), "No origin data"
                origin = self.catalog[0].origins[0]
                self._rotateZRT(st, origin)
            except Exception as e:
                self.widgets.qToolButton_rotateZRT.setChecked(False)
                err = str(e)
                err += "\nError during rotating to ZRT. Showing unrotated data."
                self.error(err)
        # check if trigger should be performed
        if self.widgets.qToolButton_trigger.isChecked():
            try:
                self._trigger(st)
            except:
                self.widgets.qToolButton_trigger.setChecked(False)
                err = "Error during triggering. Showing waveform data."
                self.error(err)

    def updatePlot(self, keep_ylims=True):
        """
        Update plot with current streams data.
        """
        ylims = [list(ax.get_ylim()) for ax in self.axs]
        self.updateIds("blue")
        # Update all plots' y data
        for tr, plot in zip(self.getCurrentStream(), self.plts):
            plot.set_ydata(tr.data)
        if keep_ylims:
            for ax, ylims_ in zip(self.axs, ylims):
                ax.set_ylim(ylims_)
        else:
            for ax in self.axs:
                ax.relim()
                ax.autoscale(axis="y", enable=True)
                ax.autoscale_view(scalex=False)
                ax.autoscale(axis="y", enable=False)
        self.redraw()

    # Define the event that handles the setting of P- and S-wave picks
    # XXX prefix with underscores to avoid autoconnect to Qt
    def __mpl_keyPressEvent(self, ev):
        if self.widgets.qToolButton_showMap.isChecked():
            return
        if self.widgets.qToolButton_overview.isChecked():
            return
        keys = self.keys
        phase_type = str(self.widgets.qComboBox_phaseType.currentText())
        st = self.getCurrentStream()
        if ev.inaxes:
            tr = st[self.axs.index(ev.inaxes)]
            pick = self.getPick(phase_hint=phase_type, seed_string=tr.id,
                                axes=ev.inaxes)
            if pick is not None:
                extra = pick.setdefault("extra", AttribDict())
            amplitude = self.getAmplitude(seed_string=tr.id, axes=ev.inaxes)
        else:
            tr = None
            pick = None
            extra = None
            amplitude = None

        #######################################################################
        # Start of key events related to picking                              #
        #######################################################################
        # For some key events (picking events) we need information on the x/y
        # position of the cursor:
        if ev.key in [keys['setPick'], keys['setPickError'],
                      keys['setMagMin'], keys['setMagMax'],
                      keys['setWeight0'], keys['setWeight1'],
                      keys['setWeight2'], keys['setWeight3'],
                      keys['setPolU'], keys['setPolD'],
                      keys['setOnsetI'],keys['setOnsetE'],
                      keys['delPick'], keys['delMagMinMax']]:
            # some keyPress events only make sense inside our matplotlib axes
            if ev.inaxes not in self.axs:
                return
            # self.setXMLEventID()

        if ev.key in [keys['setPick'], keys['setPickError'],
                      keys['setMagMin'], keys['setMagMax'],
                      keys['setWeight0'], keys['setWeight1'],
                      keys['setWeight2'], keys['setWeight3'],
                      keys['setPolU'], keys['setPolD'],
                      keys['setOnsetI'],keys['setOnsetE'],
                      keys['delPick'], keys['delMagMinMax']]:
            # some keyPress events only make sense inside our matplotlib axes
            if ev.inaxes not in self.axs:
                return
            # get the correct sample times array for the click
            t = self.t[self.axs.index(ev.inaxes)]
            tr = st[self.axs.index(ev.inaxes)]
            # We want to round from the picking location to
            # the time value of the nearest sample:
            samp_rate = tr.stats.sampling_rate
            pickSample = (ev.xdata - t[0]) * samp_rate
            self.debug(str(pickSample))
            pickSample = int(round(pickSample))
            self.debug(str(pickSample))
            # we need the position of the cursor location
            # in the seismogram array:
            xpos = pickSample
            # Determine the time of the nearest sample
            pickSample = t[pickSample]
            self.debug(str(pickSample))
            self.debug(str(ev.inaxes.lines[0].get_ydata()[xpos]))

        if ev.key == keys['setPick']:
            if phase_type in self.seismic_phases:
                pick = self.getPick(axes=ev.inaxes, phase_hint=phase_type,
                                    setdefault=True, seed_string=tr.id)
                self.debug(map(str, [ev.inaxes, self.axs, phase_type, tr.id]))
                self.info(str(pick))
                pick.setTime(self.time_rel2abs(pickSample))
                #self.updateAxes(ev.inaxes)
                self.updateAllItems()
                self.redraw()
                self.info("%s pick set at %.3f (%s)" % (phase_type,
                                                        self.time_abs2rel(pick.time),
                                                        pick.time.isoformat()))
                net = pick.waveform_id.network_code
                sta = pick.waveform_id.station_code
                phase_hint2 = {'P': 'S', 'S': 'P'}.get(pick.phase_hint, None)
                if phase_hint2:
                    pick2 = self.getPick(network=net, station=sta,
                                         phase_hint=phase_hint2)
                    if pick2:
                        self.critical("S-P time: %.3f" % abs(pick.time - pick2.time))
                return

        if ev.key in (keys['setWeight0'], keys['setWeight1'],
                      keys['setWeight2'], keys['setWeight3']):
            if phase_type in self.seismic_phases:
                if pick is None:
                    return
                if ev.key == keys['setWeight0']:
                    value = 0
                elif ev.key == keys['setWeight1']:
                    value = 1
                elif ev.key == keys['setWeight2']:
                    value = 2
                elif ev.key == keys['setWeight3']:
                    value = 3
                else:
                    raise NotImplementedError()
                extra.weight = {'value': value,
                                'namespace': NAMESPACE}
                self.updateAllItems()
                self.redraw()
                self.info("%s weight set to %i" % (phase_type, value))
                return

        if ev.key in (keys['setPolU'], keys['setPolD']):
            if phase_type in self.seismic_phases:
                if pick is None:
                    return
                if ev.key == keys['setPolU']:
                    value = "positive"
                elif ev.key == keys['setPolD']:
                    value = "negative"
                else:
                    raise NotImplementedError()
                # XXX TODO map SH/SV polarities to left/right if rotated to ZRT
                #if phase_type == "S":
                #    try:
                #        comp = ev.inaxes.texts[0].get_text()[-1].upper()
                #        value = S_POL_MAP_ZRT[comp][value]
                #        self.info("setting polarity for %s" % S_POL_PHASE_TYPE[comp])
                #    except:
                #        err = "Warning: to map up/down polarity to SH/SV " + \
                #              "equivalents rotate to ZRT and place mouse " + \
                #              "over R or T axes."
                #        self.error(err)
                pick.polarity = value
                self.updateAllItems()
                self.redraw()
                self.info("%s polarity set to %s" % (phase_type, value))
                return

        if ev.key in (keys['setOnsetI'], keys['setOnsetE']):
            if phase_type in self.seismic_phases:
                if pick is None:
                    return
                if ev.key == keys['setOnsetI']:
                    pick.onset = "impulsive"
                elif ev.key == keys['setOnsetE']:
                    value = "emergent"
                else:
                    raise NotImplementedError()
                self.updateAllItems()
                self.redraw()
                self.info("%s onset set to %s" % (phase_type, pick.onset))
                return

        if ev.key == keys['delPick']:
            if phase_type in self.seismic_phases:
                self.delPick(pick)
                self.updateAllItems()
                self.redraw()
                return

        if ev.key == keys['setPickError']:
            if phase_type in self.seismic_phases:
                if pick is None or not pick.time:
                    return
                pick.setErrorTime(self.time_rel2abs(pickSample))
                self.updateAllItems()
                self.redraw()
                self.info("%s error pick set at %s" % (phase_type,
                                                       self.time_rel2abs(pickSample).isoformat()))
                return

        if ev.key in (keys['setMagMin'], keys['setMagMax']):
            if self.widgets.qToolButton_physical_units.isChecked():
                self.error("Can only set amplitude pick on raw count data!")
                return
            # some keyPress events only make sense inside our matplotlib axes
            if not ev.inaxes in self.axs:
                return
            if phase_type == 'Mag':
                picker_width = self.config.getint("base", "magnitude_picker_width")
                ampl = self.getAmplitude(axes=ev.inaxes, setdefault=True, seed_string=tr.id)
                ampl.set_general_info()
                # do the actual work
                ydata = ev.inaxes.lines[0].get_ydata() #get the first line hoping that it is the seismogram!
                cutoffSamples = xpos - picker_width #remember, how much samples there are before our small window! We have to add this number for our MagMinT estimation!
                if ev.key == keys['setMagMin']:
                    val = np.min(ydata[xpos-picker_width:xpos+picker_width])
                    tmp_magtime = cutoffSamples + np.argmin(ydata[xpos-picker_width:xpos+picker_width])
                elif ev.key == keys['setMagMax']:
                    val = np.max(ydata[xpos-picker_width:xpos+picker_width])
                    tmp_magtime = cutoffSamples + np.argmax(ydata[xpos-picker_width:xpos+picker_width])
                # XXX TODO GSE calib handling! special handling for GSE2 data: apply calibration
                if tr.stats._format == "GSE2":
                    val = val / (tr.stats.calib * 2 * np.pi / tr.stats.gse2.calper)
                # save time of magnitude minimum in seconds
                tmp_magtime = self.time_rel2abs(t[tmp_magtime])
                if ev.key == keys['setMagMin']:
                    ampl.setLow(tmp_magtime, val)
                elif ev.key == keys['setMagMax']:
                    ampl.setHigh(tmp_magtime, val)
                self.updateMagnitude()
                self.updateAllItems()
                self.redraw()
                return

        if ev.key == keys['delMagMinMax']:
            # some keyPress events only make sense inside our matplotlib axes
            if not ev.inaxes in self.axs:
                return
            if phase_type == 'Mag':
                if amplitude is not None:
                    self.delAmplitude(amplitude)
                    self.updateMagnitude()
                    self.updateAllItems()
                    self.redraw()
                return
        #######################################################################
        # End of key events related to picking                                #
        #######################################################################

        if ev.key in (keys['switchWheelZoomAxis'], keys['scrollWheelZoom']):
            return

        # iterate the phase type combobox
        if ev.key == keys['switchPhase']:
            combobox = self.widgets.qComboBox_phaseType
            next = (combobox.currentIndex() + 1) % combobox.count()
            combobox.setCurrentIndex(next)
            self.info("Switching Phase button")
            return

        if ev.key == keys['prevStream']:
            if self.widgets.qToolButton_overview.isChecked():
                return
            self.on_qToolButton_previousStream_clicked()
            return

        if ev.key == keys['nextStream']:
            if self.widgets.qToolButton_overview.isChecked():
                return
            self.on_qToolButton_nextStream_clicked()
            return

    # Define zooming for the mouse wheel wheel
    def __mpl_wheelEvent(self, ev):
        # create mpl event from QEvent to get cursor position in data coords
        x = ev.x()
        y = self.canv.height() - ev.y()
        mpl_ev = MplMouseEvent("scroll_event", self.canv, x, y, "up", guiEvent=ev)
        # Calculate and set new axes boundaries from old ones
        if self.widgets.qToolButton_showMap.isChecked():
            ax = self.axEventMap
        else:
            ax = self.axs[0]
        (left, right) = ax.get_xbound()
        (bottom, top) = ax.get_ybound()
        # Get the keyboard modifiers. They are a enum type.
        # Use bitwise or to compare...hope this is correct.
        if ev.modifiers() == QtCore.Qt.NoModifier:
            # Zoom in.
            if ev.angleDelta().y() < 0:
                left -= (mpl_ev.xdata - left) / 2
                right += (right - mpl_ev.xdata) / 2
                if self.widgets.qToolButton_showMap.isChecked():
                    top -= (mpl_ev.ydata - top) / 2
                    bottom += (bottom - mpl_ev.ydata) / 2
            # Zoom out.
            elif ev.angleDelta().y() > 0:
                left += (mpl_ev.xdata - left) / 2
                right -= (right - mpl_ev.xdata) / 2
                if self.widgets.qToolButton_showMap.isChecked():
                    top += (mpl_ev.ydata - top) / 2
                    bottom -= (bottom - mpl_ev.ydata) / 2
        # Still able to use the dictionary.
        elif ev.modifiers() == getattr(QtCore.Qt,
                '%sModifier' % self.keys['switchWheelZoomAxis'].capitalize()):
            if self.widgets.qToolButton_spectrogram.isChecked():
            # Zoom in on wheel-up
                if ev.angleDelta().y() < 0:
                    top -= (mpl_ev.ydata - top) / 2
                    bottom += (bottom - mpl_ev.ydata) / 2
                # Zoom out on wheel-down
                elif ev.angleDelta().y() > 0:
                    top += (mpl_ev.ydata - top) / 2
                    bottom -= (bottom - mpl_ev.ydata) / 2
            else:
            # Zoom in on wheel-up
                if ev.angleDelta().y() < 0:
                    top *= 2
                    bottom *= 2
                # Zoom out on wheel-down
                elif ev.angleDelta().y() > 0:
                    top /= 2
                    bottom /= 2
        # Still able to use the dictionary.
        elif ev.modifiers() == getattr(
                QtCore.Qt,
                '%sModifier' % self.keys['scrollWheelZoom'].capitalize()):
            direction = (self.config.getboolean('misc', 'scrollWheelInvert')
                         and 1 or -1)
            shift = ((right - left) *
                     self.config.getfloat('misc', 'scrollWheelPercentage'))
            if self.widgets.qToolButton_showMap.isChecked():
                pass
            else:
                # scroll left
                if ev.angleDelta() * direction < 0:
                    left -= shift
                    right -= shift
                # scroll right
                elif ev.angleDelta() * direction > 0:
                    left += shift
                    right += shift
        ax.set_xbound(lower=left, upper=right)
        ax.set_ybound(lower=bottom, upper=top)
        self.redraw()

    # Define zoom reset for the mouse button 2 (always wheel wheel!?)
    def __mpl_mouseButtonPressEvent(self, ev):
        if self.widgets.qToolButton_showMap.isChecked():
            return
        if self.widgets.qToolButton_overview.isChecked():
            return
        # set widgetlock when pressing mouse buttons and dont show cursor
        # cursor should not be plotted when making a zoom selection etc.
        if ev.button in [1, 3]:
            self.multicursor.visible = False
            # reuse this event as setPick / setPickError event
            if ev.button == 1:
                if str(self.widgets.qComboBox_phaseType.currentText()) in self.seismic_phases:
                    ev.key = self.keys['setPick']
                else:
                    ev.key = self.keys['setMagMin']
            elif ev.button == 3:
                if str(self.widgets.qComboBox_phaseType.currentText()) in self.seismic_phases:
                    ev.key = self.keys['setPickError']
                else:
                    ev.key = self.keys['setMagMax']
            self.__mpl_keyPressEvent(ev)
            # XXX self.canv.widgetlock(self.toolbar)
        # show traces from start to end
        # (Use Z trace limits as boundaries)
        elif ev.button == 2:
            if self.widgets.qToolButton_showMap.isChecked():
                ax = self.axEventMap
            else:
                ax = self.axs[0]
            ax.set_xbound(lower=self.xMin, upper=self.xMax)
            ax.set_ybound(lower=self.yMin, upper=self.yMax)
            # Update all subplots
            self.redraw()
            self.info("Resetting axes")

    def __mpl_mouseButtonReleaseEvent(self, ev):
        if self.widgets.qToolButton_showMap.isChecked():
            return
        if self.widgets.qToolButton_overview.isChecked():
            return
        # release widgetlock when releasing mouse buttons
        if ev.button in [1, 3]:
            self.multicursor.visible = True
            # XXX self.canv.widgetlock.release(self.toolbar)

    def __mpl_motionNotifyEvent(self, ev):
        try:
            if ev.inaxes in self.axs:
                self.widgets.qLabel_xdata_rel.setText(formatXTicklabels(ev.xdata))
                label = self.time_rel2abs(ev.xdata).isoformat().replace("T", "  ")[:-3]
                self.widgets.qLabel_xdata_abs.setText(label)
                if self.widgets.qToolButton_physical_units.isChecked() \
                        and not self.widgets.qToolButton_spectrogram.isChecked():
                    absval = abs(ev.ydata)
                    units = str(self.widgets.qToolButton_physical_units.text()).strip('[]')
                    if absval >= 1:
                        text = "{:.3g} {}".format(ev.ydata, units)
                    elif absval >= 1e-4:
                        # prepend milli-
                        units = 'm' + units
                        text = "{:.3g} {}".format(ev.ydata * 1e3, units)
                    elif absval >= 1e-7:
                        # prepend micro-
                        units = 'mu' + units
                        text = "{:.3g} {}".format(ev.ydata * 1e6, units)
                    else:
                        # prepend nano-
                        units = 'n' + units
                        text = "{:.3g} {}".format(ev.ydata * 1e9, units)
                    self.widgets.qLabel_ydata.setText(text)
                else:
                    self.widgets.qLabel_ydata.setText("%.1f" % ev.ydata)
            else:
                self.widgets.qLabel_xdata_rel.setText("")
                self.widgets.qLabel_xdata_abs.setText(str(ev.xdata))
                self.widgets.qLabel_ydata.setText(str(ev.ydata))
        except TypeError:
            pass

    #lookup multicursor source: http://matplotlib.sourcearchive.com/documentation/0.98.1/widgets_8py-source.html
    def multicursorReinit(self):
        self.multicursor.__init__(self.canv, self.axs, useblit=True,
                                  color='black', linewidth=AXVLINEWIDTH,
                                  ls='dotted')
        self.updateMulticursorColor()
        # XXX self.canv.widgetlock.release(self.toolbar)

    def updateMulticursorColor(self):
        phase_name = str(self.widgets.qComboBox_phaseType.currentText())
        if phase_name == 'Mag':
            color = self._magnitude_color
        else:
            color = self.seismic_phases[phase_name]
        for l in self.multicursor.lines:
            l.set_color(color)

    def updateStreamNumberLabel(self):
        label = "%02i/%02i" % (self.stPt + 1, self.stNum)
        self.widgets.qLabel_streamNumber.setText(label)

    def updateStreamNameCombobox(self):
        self.widgets.qComboBox_streamName.setCurrentIndex(self.stPt)

    def updateStreamLabels(self):
        self.updateStreamNumberLabel()
        self.updateStreamNameCombobox()

    def update_stream_name_combobox_from_streams(self, suffixes=None):
        """
        Updates the dropdown stream label list, e.g. when streams were sorted
        by epicentral distance.

        Optionally, a list of string suffixes can be supplied, to be appended to the
        stream label (e.g. with epicentral distance info). Obviously must be
        of same length as stream list.
        """
        if suffixes is not None:
            if len(suffixes) != len(self.streams_bkp):
                err = 'Error: suffix list must have same length as stream list!'
                self.error(err)
                suffixes = None
        self.widgets.qComboBox_streamName.clear()
        labels = ["%s.%s" % (st[0].stats.network, st[0].stats.station) \
                  for st in self.streams_bkp]
        if suffixes is not None:
            labels = ["%s %s" % (l, s) for l, s in zip(labels, suffixes)]
        self.widgets.qComboBox_streamName.addItems(labels)

    def doFocmec(self):
        prog_dict = PROGRAMS['focmec']
        files = prog_dict['files']
        print(prog_dict)
        print(files)
        #Fortran style! 1: Station 2: Azimuth 3: Incident 4: Polarity
        #fmt = "ONTN  349.00   96.00C"
        fmt = "%4s  %6.2f  %6.2f%1s\n"
        count = 0
        polarities = []
        for pick in self.catalog[0].picks:
            arrival = getArrivalForPick(self.catalog[0].origins[0].arrivals,
                                        pick)
            if arrival is None:
                self.critical("focmec: No arrival for pick! Run location "
                              "routine again after changing/adding picks! "
                              "Skipping:\n%s" % pick)
                continue
            pt = pick.phase_hint
            if pick.polarity is None:
                self.critical("focmec: Pick missing polarity. "
                              "Skipping:\n%s" % pick)
                continue
            if arrival.azimuth is None:
                self.critical("focmec: Arrival missing azimuth. "
                              "Skipping:\n%s" % arrival)
                continue
            if arrival.takeoff_angle is None:
                self.critical("focmec: Arrival missing takeoff angle. "
                              "Skipping:\n%s" % arrival)
                continue
            sta = pick.waveform_id.station_code
            comp = pick.waveform_id.channel_code[-1]
            # XXX commenting the following out again
            # XXX only polarities with Azim/Inci info from location used
            #if pt + 'Azim' not in dict or pt + 'Inci' not in dict:
            #    azim, bazim, inci = coords2azbazinc(st, self.dictOrigin)
            #    err = "Warning: No azimuth/incidence information for " + \
            #          "phase pick found, using azimuth/incidence from " + \
            #          "source/receiver geometry."
            #    self.error(err)
            # XXX hack for nonlinloc: they return different angles:
            # XXX they use takeoff dip instead of incidence
            #elif self.dictOrigin['Program'] == "NLLoc":
            #    azim, bazim, inci = coords2azbazinc(st, self.dictOrigin)
            #    err = "Warning: Location program is nonlinloc, " + \
            #          "returning takeoff angles instead of incidence " + \
            #          "angles. Using azimuth/incidence from " + \
            #          "source/receiver geometry."
            #    self.error(err)
            #else:
            azim = arrival.azimuth
            inci = arrival.takeoff_angle
            pol = pick.polarity
            try:
                pol = POLARITY_2_FOCMEC[comp][pol]
            except:
                err = "Error: Failed to map polarity information to " + \
                      "FOCMEC identifier (%s, %s, %s), skipping."
                err = err % (pick.waveform_id.station_code, pt, pol)
                self.error(err)
                continue
            count += 1
            polarities.append((sta, azim, inci, pol))
        sta_map = self._4_letter_sta_map
        with open(files['phases'], 'wt') as f:
            f.write("\n") #first line is ignored!
            for sta, azim, inci, pol in polarities:
                f.write(fmt % (sta_map[sta], azim, inci, pol))
        self.critical('Phases for focmec: %i' % count)
        self.catFile(files['phases'], self.critical)
        call = prog_dict['Call']
        (msg, err, returncode) = call(prog_dict)
        self.info(msg)
        self.error(err)
        if returncode == 1:
            err = "Error: focmec did not find a suitable solution!"
            self.error(err)
            return
        self.critical('--> focmec finished')
        lines = open(files['summary'], "rt").readlines()
        self.critical('%i suitable solutions found:' % len(lines))
        print(lines)
        fms = []
        for line in lines:
            line = line.split()
            np1 = NodalPlane()
            np = NodalPlanes()
            fm = FocalMechanism()
            fm.nodal_planes = np
            fm.nodal_planes.nodal_plane_1 = np1
            fm.method_id = "/".join([ID_ROOT, "focal_mechanism_method", "focmec", "2"])
            np1.dip = float(line[0])
            np1.strike = float(line[1])
            np1.rake = float(line[2])
            fm.station_polarity_count = count
            errors = sum([int(float(line[no])) for no in (3, 4, 5)]) # not used in xml
            fm.misfit = errors / float(fm.station_polarity_count)
            fm.comments.append(Comment(text="Possible Solution Count: %i" % len(lines)))
            fm.comments.append(Comment(text="Polarity Errors: %i" % errors))
            self.critical("Strike: %6.2f  Dip: %6.2f  Rake: %6.2f  Polarity Errors: %i/%i" % \
                      (np1.strike, np1.dip, np1.rake, errors, count))
            fms.append(fm)
        self.catalog[0].focal_mechanisms = fms
        self.focMechCurrent = 0
        self.critical("selecting Focal Mechanism No.  1 of %2i:" % len(fms))
        fm = fms[self.focMechCurrent]
        self.catalog[0].preferred_focal_mechanism_id = str(fm.resource_id)
        np1 = fm.nodal_planes.nodal_plane_1
        self.critical("Strike: %6.2f  Dip: %6.2f  Rake: %6.2f  Misfit: %.2f" % \
                      (np1.strike, np1.dip, np1.rake, fm.misfit))

    def nextFocMec(self):
        fms = self.catalog[0].focal_mechanisms
        self.focMechCurrent = (self.focMechCurrent + 1) % len(fms)
        fm = fms[self.focMechCurrent]
        np1 = fm.nodal_planes.nodal_plane_1
        self.catalog[0].preferred_focal_mechanism_id = str(fm.resource_id)
        self.critical("selecting Focal Mechanism No. %2i of %2i:" % \
                      (self.focMechCurrent + 1, len(fms)))
        self.critical("Strike: %6.2f  Dip: %6.2f  Rake: %6.2f  Misfit: %.2f" % \
                      (np1.strike, np1.dip, np1.rake, fm.misfit))

    def drawFocMec(self):
        fms = self.catalog[0].focal_mechanisms
        if not fms:
            err = "Error: No focal mechanism data!"
            self.error(err)
            return
        # make up the figure:
        fig = self.fig
        ax = fig.add_subplot(111, aspect="equal")
        axs = [ax]
        self.axsFocMec = axs
        #ax.autoscale_view(tight=False, scalex=True, scaley=True)
        width = 2
        plot_width = width * 0.95
        #plot_width = 0.95 * width
        fig.subplots_adjust(left=0, bottom=0, right=1, top=1)
        # plot the selected solution
        fm = fms[self.focMechCurrent]
        np1 = fm.nodal_planes.nodal_plane_1
        beach_ = beach([np1.strike, np1.dip, np1.rake],
                       width=plot_width)
        ax.add_collection(beach_)
        # plot the alternative solutions
        for fm_ in fms:
            _np1 = fm_.nodal_planes.nodal_plane_1
            beach_ = beach([_np1.strike, _np1.dip, _np1.rake],
                           nofill=True, edgecolor='k', linewidth=1.,
                           alpha=0.3, width=plot_width)
            ax.add_collection(beach_)
        text = "Focal Mechanism (%i of %i)" % \
               (self.focMechCurrent + 1, len(fms))
        text += "\nStrike: %6.2f  Dip: %6.2f  Rake: %6.2f" % \
                (np1.strike, np1.dip, np1.rake)
        if fm.misfit:
            text += "\nMisfit: %.2f" % fm.misfit
        if fm.station_polarity_count:
            text += "\nStation Polarity Count: %i" % fm.station_polarity_count
        #fig.canvas.set_window_title("Focal Mechanism (%i of %i)" % \
        #        (self.focMechCurrent + 1, len(fms)))
        fig.subplots_adjust(top=0.88) # make room for suptitle
        # values 0.02 and 0.96 fit best over the outer edges of beachball
        #ax = fig.add_axes([0.00, 0.02, 1.00, 0.96], polar=True)
        ax.set_ylim(-1, 1)
        ax.set_xlim(-1, 1)
        ax.axison = False
        self.axFocMecStations = fig.add_axes([0.00,0.02,1.00,0.84], polar=True)
        ax = self.axFocMecStations
        ax.set_title(text)
        ax.set_axis_off()
        azims = []
        incis = []
        polarities = []
        bbox = dict(boxstyle="round,pad=0.2", fc="w", ec="k", lw=1.5,
                    alpha=0.7)
        for pick in self.catalog[0].picks:
            if pick.phase_hint != "P":
                continue
            wid = pick.waveform_id
            net = wid.network_code
            sta = wid.station_code
            arrival = getArrivalForPick(self.catalog[0].origins[0].arrivals, pick)
            if not pick:
                continue
            if pick.polarity is None or arrival is None or arrival.azimuth is None or arrival.takeoff_angle is None:
                continue
            if pick.polarity == "positive":
                polarity = True
            elif pick.polarity == "negative":
                polarity = False
            else:
                polarity = None
            azim = arrival.azimuth
            inci = arrival.takeoff_angle
            # lower hemisphere projection
            if inci > 90:
                inci = 180. - inci
                azim = -180. + azim
            #we have to hack the azimuth because of the polar plot
            #axes orientation
            plotazim = (np.pi / 2.) - ((azim / 180.) * np.pi)
            azims.append(plotazim)
            incis.append(inci)
            polarities.append(polarity)
            ax.text(plotazim, inci, "  " + sta, va="top", bbox=bbox, zorder=2)
        azims = np.array(azims)
        incis = np.array(incis)
        polarities = np.array(polarities, dtype=bool)
        ax.scatter(azims, incis, marker="o", lw=2, facecolor="w",
                   edgecolor="k", s=200, zorder=3)
        mask = (polarities == True)
        ax.scatter(azims[mask], incis[mask], marker="+", lw=3, color="k",
                   s=200, zorder=4)
        mask = ~mask
        ax.scatter(azims[mask], incis[mask], marker="_", lw=3, color="k",
                   s=200, zorder=4)
        #this fits the 90 degree incident value to the beachball edge best
        ax.set_ylim([0., 91])
        self.canv.draw()

    def delFocMec(self):
        if hasattr(self, "axFocMecStations"):
            self.fig.delaxes(self.axFocMecStations)
            del self.axFocMecStations
        if hasattr(self, "axsFocMec"):
            for ax in self.axsFocMec:
                if ax in self.fig.axes:
                    self.fig.delaxes(ax)
                del ax

    def _setup_4_letter_station_map(self):
        # make sure the 4-letter station codes are unique
        sta_map_tmp = {}
        for sta in [st[0].stats.station for st in self.streams_bkp]:
            sta_map_tmp.setdefault(sta[:4], set()).add(sta)
        sta_map = {}
        sta_map_reverse = {}
        for sta_short, stations in sta_map_tmp.items():
            stations = list(stations)
            if len(stations) == 1:
                sta_map[stations[0]] = sta_short
                sta_map_reverse[sta_short] = stations[0]
            else:
                if len(stations) <= 10:
                    for i, sta in enumerate(stations):
                        short_name = sta[:2] + "_%i" % i
                        sta_map[sta] = short_name
                        sta_map_reverse[short_name] = sta
                else:
                    for i, sta in enumerate(stations):
                        short_name = sta[0] + "_%02i" % i
                        sta_map[sta] = short_name
                        sta_map_reverse[short_name] = sta
        self._4_letter_sta_map = sta_map
        self._4_letter_sta_map_reverse = sta_map_reverse

    def doHyp2000(self):
        """
        Writes input files for hyp2000 and starts the hyp2000 program via a
        system call.
        """
        prog_dict = PROGRAMS['hyp_2000']
        files = prog_dict['files']
        precall = prog_dict['PreCall']
        precall(prog_dict)

        f = open(files['phases'], 'wt')
        phases_hypo71 = self.dicts2hypo71Phases()
        f.write(phases_hypo71)
        f.close()

        f2 = open(files['stations'], 'wt')
        stations_hypo71 = self.dicts2hypo71Stations()
        f2.write(stations_hypo71)
        f2.close()

        self.critical('Phases for Hypo2000:')
        self.catFile(files['phases'], self.critical)
        self.critical('Stations for Hypo2000:')
        self.catFile(files['stations'], self.critical)

        call = prog_dict['Call']
        (msg, err, returncode) = call(prog_dict)
        self.info(msg)
        self.error(err)
        self.critical('--> hyp2000 finished')
        self.catFile(files['summary'], self.critical)

    def doNLLoc(self):
        """
        Writes input files for NLLoc and starts the NonLinLoc program via a
        system call.
        """
        prog_dict = PROGRAMS['nlloc']
        files = prog_dict['files']
        # determine which model should be used in location
        controlfilename = "locate_%s.nlloc" % \
                          str(self.widgets.qComboBox_nllocModel.currentText())

        precall = prog_dict['PreCall']
        precall(prog_dict)
        f = open(files['phases'], 'wt')
        phases_nlloc = self.dicts2NLLocPhases()
        f.write(phases_nlloc)
        f.close()
        self.critical('Phases for NLLoc:')
        self.catFile(files['phases'], self.critical)

        call = prog_dict['Call']
        (msg, err, returncode) = call(prog_dict, controlfilename)
        self.info(msg)
        self.error(err)
        self.critical('--> NLLoc finished')
        self.catFile(files['summary'], self.critical)

    def catFile(self, file, logfunct):
        lines = open(file, "rt").readlines()
        msg = ""
        for line in lines:
            msg += line
        logfunct(msg)

    def loadNLLocOutput(self):
        files = PROGRAMS['nlloc']['files']
        lines = open(files['summary'], "rt").readlines()
        if not lines:
            err = "Error: NLLoc output file (%s) does not exist!" % \
                    files['summary']
            self.error(err)
            return
        # goto signature info line
        try:
            line = lines.pop(0)
            while not line.startswith("SIGNATURE"):
                line = lines.pop(0)
        except:
            err = "Error: No correct location info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return

        line = line.rstrip().split('"')[1]
        signature, nlloc_version, date, time = line.rsplit(" ", 3)
        # new NLLoc > 6.0 seems to add prefix 'run:' before date
        if date.startswith('run:'):
            date = date[4:]
        saved_locale = locale.getlocale()
        try:
            locale.setlocale(locale.LC_ALL, ('en_US', 'UTF-8'))
        except:
            creation_time = None
        else:
            creation_time = UTCDateTime().strptime(date + time,
                                                   str("%d%b%Y%Hh%Mm%S"))
        finally:
            locale.setlocale(locale.LC_ALL, saved_locale)
        # goto maximum likelihood origin location info line
        try:
            line = lines.pop(0)
            while not line.startswith("HYPOCENTER"):
                line = lines.pop(0)
        except:
            err = "Error: No correct location info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return

        line = line.split()
        x = float(line[2])
        y = float(line[4])
        # depth = - float(line[6]) # depth: negative down!
        # CJH I reported depths at SURF in meters below 130 m so positive is
        # down in this case
        depth = float(line[6])

        # lon, lat = gk2lonlat(x, y)
        # Convert coords
        print('Doing hypo conversion')
        # Descale first
        depth *= 10
        lon, lat = surf_xyz2latlon(np.array([x]), np.array([y]))
        print(lon, lat)
        # goto origin time info line
        try:
            line = lines.pop(0)
            while not line.startswith("GEOGRAPHIC  OT"):
                line = lines.pop(0)
        except:
            err = "Error: No correct location info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return
        line = line.split()
        year = int(line[2])
        month = int(line[3])
        day = int(line[4])
        hour = int(line[5])
        minute = int(line[6])
        seconds = float(line[7])
        time = UTCDateTime(year, month, day, hour, minute, seconds)
        # Convert to actual time
        time = UTCDateTime(datetime.fromtimestamp(
            time.datetime.timestamp() / 100.
        ))
        # goto location quality info line
        try:
            line = lines.pop(0)
            while not line.startswith("QUALITY"):
                line = lines.pop(0)
        except:
            err = "Error: No correct location info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return

        line = line.split()
        rms = float(line[8])
        gap = float(line[12])

        # goto location quality info line
        try:
            line = lines.pop(0)
            while not line.startswith("STATISTICS"):
                line = lines.pop(0)
        except:
            err = "Error: No correct location info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return
        line = line.split()
        # read in the error ellipsoid representation of the location error.
        # this is given as azimuth/dip/length of axis 1 and 2 and as length
        # of axis 3.
        azim1 = float(line[20])
        dip1 = float(line[22])
        len1 = float(line[24])
        azim2 = float(line[26])
        dip2 = float(line[28])
        len2 = float(line[30])
        len3 = float(line[32])

        # XXX TODO save original nlloc error ellipse?!
        errX, errY, errZ = errorEllipsoid2CartesianErrors(azim1, dip1, len1,
                                                          azim2, dip2, len2,
                                                          len3)

        # XXX
        # NLLOC uses error ellipsoid for 68% confidence interval relating to
        # one standard deviation in the normal distribution.
        # We multiply all errors by 2 to approximately get the 95% confidence
        # level (two standard deviations)...
        errX *= 2
        errY *= 2
        errZ *= 2
        # CJH Now descale to correct dimensions
        errX /= 100
        errY /= 100
        errZ /= 100
        # determine which model was used:
        # XXX handling of path extremely hackish! to be improved!!
        dirname = os.path.dirname(files['summary'])
        controlfile = os.path.join(dirname, "last.in")
        lines2 = open(controlfile, "rt").readlines()
        line2 = lines2.pop()
        while not line2.startswith("LOCFILES"):
            line2 = lines2.pop()
        line2 = line2.split()
        model = line2[3]
        model = model.split("/")[-1]
        catalog = self.catalog
        event = catalog[0]
        if event.creation_info is None:
            event.creation_info = CreationInfo()
            event.creation_info.creation_time = UTCDateTime()
        o = Origin()
        event.origins = [o]
        self.catalog[0].set_creation_info_username(self.username)
        # version field has 64 char maximum per QuakeML RNG schema
        o.creation_info = CreationInfo(creation_time=creation_time,
                                       version=nlloc_version[:64])
        # assign origin info
        o.method_id = "/".join([ID_ROOT, "location_method", "nlloc", "7"])
        print('Creating origin uncertainty')
        o.longitude = lon[0]
        o.latitude = lat[0]
        print('Assigning depth {}'.format(depth))
        o.depth = depth# * (-1e3)  # meters positive down!
        print('Creating extra AttribDict')
        # Attribute dict for actual hmc coords
        extra = AttribDict({
            'hmc_east': {
                'value': x,
                'namespace': 'smi:local/hmc'
            },
            'hmc_north': {
                'value': y,
                'namespace': 'smi:local/hmc'
            },
            'hmc_elev': {
                'value': 130 - depth, # Extra attribs maintain absolute elevation
                'namespace': 'smi:local/hmc'
            }
        })
        o.extra = extra
        o.origin_uncertainty = OriginUncertainty()
        o.quality = OriginQuality()
        ou = o.origin_uncertainty
        oq = o.quality
        if errY > errX:
            ou.azimuth_max_horizontal_uncertainty = 0
        else:
            ou.azimuth_max_horizontal_uncertainty = 90
        ou.min_horizontal_uncertainty, \
                ou.max_horizontal_uncertainty = \
                sorted([errX * 1e3, errY * 1e3])
        ou.preferred_description = "uncertainty ellipse"
        o.depth_errors.uncertainty = errZ * 1e3
        oq.standard_error = rms #XXX stimmt diese Zuordnung!!!?!
        oq.azimuthal_gap = gap
        o.depth_type = "from location"
        o.earth_model_id = "%s/earth_model/%s" % (ID_ROOT, model)
        o.time = time
        # goto synthetic phases info lines
        try:
            line = lines.pop(0)
            while not line.startswith("PHASE ID"):
                line = lines.pop(0)
        except:
            err = "Error: No correct synthetic phase info found in NLLoc " + \
                  "outputfile (%s)!" % files['summary']
            self.error(err)
            return

        # remove all non phase-info-lines from bottom of list
        try:
            badline = lines.pop()
            while not badline.startswith("END_PHASE"):
                badline = lines.pop()
        except:
            err = "Error: Could not remove unwanted lines at bottom of " + \
                  "NLLoc outputfile (%s)!" % files['summary']
            self.error(err)
            return

        o.quality.used_phase_count = 0
        o.quality.extra = AttribDict()
        o.quality.extra.usedPhaseCountP = {'value': 0, 'namespace': NAMESPACE}
        o.quality.extra.usedPhaseCountS = {'value': 0, 'namespace': NAMESPACE}

        # go through all phase info lines
        """
        Order of fields:
        ID Ins Cmp On Pha FM Q Date HrMn Sec Coda Amp Per PriorWt > Err ErrMag
        TTpred Res Weight StaLoc(X Y Z) SDist SAzim RAz RDip RQual Tcorr
        TTerrTcorr

        Fields:
        ID (char*6)
            station name or code
        Ins (char*4)
            instrument identification for the trace for which the time pick corresponds (i.e. SP, BRB, VBB)
        Cmp (char*4)
            component identification for the trace for which the time pick corresponds (i.e. Z, N, E, H)
        On (char*1)
            description of P phase arrival onset; i, e
        Pha (char*6)
            Phase identification (i.e. P, S, PmP)
        FM (char*1)
            first motion direction of P arrival; c, C, u, U = compression; d, D = dilatation; +, -, Z, N; . or ? = not readable.
        Date (yyyymmdd) (int*6)
            year (with century), month, day
        HrMn (hhmm) (int*4)
            Hour, min
        Sec (float*7.4)
            seconds of phase arrival
        Err (char*3)
            Error/uncertainty type; GAU
        ErrMag (expFloat*9.2)
            Error/uncertainty magnitude in seconds
        Coda (expFloat*9.2)
            coda duration reading
        Amp (expFloat*9.2)
            Maxumim peak-to-peak amplitude
        Per (expFloat*9.2)
            Period of amplitude reading
        PriorWt (expFloat*9.2)
            A-priori phase weight
        > (char*1)
            Required separator between first part (observations) and second part (calculated values) of phase record.
        TTpred (float*9.4)
            Predicted travel time
        Res (float*9.4)
            Residual (observed - predicted arrival time)
        Weight (float*9.4)
            Phase weight (covariance matrix weight for LOCMETH GAU_ANALYTIC, posterior weight for LOCMETH EDT EDT_OT_WT)
        StaLoc(X Y Z) (3 * float*9.4)
            Non-GLOBAL: x, y, z location of station in transformed, rectangular coordinates
            GLOBAL: longitude, latitude, z location of station
        SDist (float*9.4)
            Maximum likelihood hypocenter to station epicentral distance in kilometers
        SAzim (float*6.2)
            Maximum likelihood hypocenter to station epicentral azimuth in degrees CW from North
        RAz (float*5.1)
            Ray take-off azimuth at maximum likelihood hypocenter in degrees CW from North
        RDip (float*5.1)
            Ray take-off dip at maximum likelihood hypocenter in degrees upwards from vertical down (0 = down, 180 = up)
        RQual (float*5.1)
            Quality of take-off angle estimation (0 = unreliable, 10 = best)
        Tcorr (float*9.4)
            Time correction (station delay) used for location
        TTerr (expFloat*9.2)
            Traveltime error used for location
        """
        used_stations = set()
        for line in lines:
            line = line.split()
            # check which type of phase
            if line[4] == "P":
                type = "P"
            elif line[4] == "S":
                type = "S"
            else:
                self.error("Encountered a phase that is not P and not S!! "
                           "This case is not handled yet in reading NLLOC "
                           "output...")
                continue
            # get values from line
            station = line[0]
            epidist = float(line[21])
            azimuth = float(line[23])
            ray_dip = float(line[24])
            # if we do the location on traveltime-grids without angle-grids we
            # do not get ray azimuth/incidence. but we can at least use the
            # station to hypocenter azimuth which is very close (~2 deg) to the
            # ray azimuth
            if azimuth == 0.0 and ray_dip == 0.0:
                azimuth = float(line[22])
                ray_dip = np.nan
            if line[3] == "I":
                onset = "impulsive"
            elif line[3] == "E":
                onset = "emergent"
            else:
                onset = None
            if line[5] == "U":
                polarity = "positive"
            elif line[5] == "D":
                polarity = "negative"
            else:
                polarity = None
            # predicted travel time is zero.
            # seems to happen when no travel time cube is present for a
            # provided station reading. show an error message and skip this
            # arrival.
            if float(line[15]) == 0.0:
                msg = ("Predicted travel time for station '%s' is zero. "
                       "Most likely the travel time cube is missing for "
                       "this station! Skipping arrival for this station.")
                self.error(msg % station)
                continue
            res = float(line[16])
            weight = float(line[17])

            # assign synthetic phase info
            pick = self.getPick(station=station, phase_hint=type)
            if pick is None:
                msg = "This should not happen! Location output was read and a corresponding pick is missing!"
                raise NotImplementedError(msg)
            arrival = Arrival(origin=o, pick=pick)
            # residual is defined as P-Psynth by NLLOC!
            arrival.distance = kilometer2degrees(epidist)
            arrival.phase = type
            # arrival.time_residual = res
            arrival.time_residual = res / 1000. # CJH descale time too (why 1000)??
            arrival.azimuth = azimuth
            if not np.isnan(ray_dip):
                arrival.takeoff_angle = ray_dip
            if onset and not pick.onset:
                pick.onset = onset
            if polarity and not pick.polarity:
                pick.polarity = polarity
            # we use weights 0,1,2,3 but NLLoc outputs floats...
            arrival.time_weight = weight
            o.quality.used_phase_count += 1
            if type == "P":
                o.quality.extra.usedPhaseCountP['value'] += 1
            elif type == "S":
                o.quality.extra.usedPhaseCountS['value'] += 1
            else:
                self.error("Phase '%s' not recognized as P or S. " % type +
                           "Not incrementing P nor S phase count.")
            used_stations.add(station)
        o.used_station_count = len(used_stations)
        self.update_origin_azimuthal_gap()
        print('Made it through location reading')
        # read NLLOC scatter file
        data = readNLLocScatter(PROGRAMS['nlloc']['files']['scatter'])
        print('Read in scatter')
        o.nonlinloc_scatter = data

    def loadHyp2000Data(self):
        sta_map_reverse = self._4_letter_sta_map_reverse
        files = PROGRAMS['hyp_2000']['files']
        lines = open(files['summary'], "rt").readlines()
        if lines == []:
            err = "Error: Hypo2000 output file (%s) does not exist!" % \
                    files['summary']
            self.error(err)
            return
        # goto origin info line
        while True:
            try:
                line = lines.pop(0)
            except:
                break
            if line.startswith(" YEAR MO DA  --ORIGIN--"):
                break
        try:
            line = lines.pop(0)
        except:
            err = "Error: No location info found in Hypo2000 outputfile " + \
                  "(%s)!" % files['summary']
            self.error(err)
            return

        year = int(line[1:5])
        month = int(line[6:8])
        day = int(line[9:11])
        hour = int(line[13:15])
        minute = int(line[15:17])
        seconds = float(line[18:23])
        time = UTCDateTime(year, month, day, hour, minute, seconds)
        lat_deg = int(line[25:27])
        lat_min = float(line[28:33])
        lat = lat_deg + (lat_min / 60.)
        if line[27] == "S":
            lat = -lat
        lon_deg = int(line[35:38])
        lon_min = float(line[39:44])
        lon = lon_deg + (lon_min / 60.)
        if line[38] == "W":
            lon = -lon
        depth = -float(line[46:51]) # depth: negative down!
        rms = float(line[52:57])
        errXY = float(line[58:63])
        errZ = float(line[64:69])

        # goto next origin info line
        while True:
            try:
                line = lines.pop(0)
            except:
                break
            if line.startswith(" NSTA NPHS  DMIN MODEL"):
                break
        line = lines.pop(0)

        #model = line[17:22].strip()
        gap = int(line[23:26])

        line = lines.pop(0)
        model = line[49:].strip()
        # this is to prevent characters that are invalid in QuakeML URIs
        # hopefully handled in the future by obspy/obspy#1018
        model = re.sub(r"[^\w\d\-\.\*\(\)\+\?_~'=,;#/&amp;]", '_', model)

        # assign origin info
        o = Origin()
        self.catalog[0].origins = [o]
        self.catalog[0].set_creation_info_username(self.username)
        o.clear()
        o.method_id = "/".join([ID_ROOT, "location_method", "hyp2000", "3"])
        o.origin_uncertainty = OriginUncertainty()
        o.quality = OriginQuality()
        ou = o.origin_uncertainty
        oq = o.quality
        o.longitude = lon
        o.latitude = lat
        o.depth = depth * (-1e3)  # meters positive down!
        # all errors are given in km!
        ou.horizontal_uncertainty = errXY * 1e3
        ou.preferred_description = "horizontal uncertainty"
        o.depth_errors.uncertainty = errZ * 1e3
        oq.standard_error = rms #XXX stimmt diese Zuordnung!!!?!
        oq.azimuthal_gap = gap
        o.depth_type = "from location"
        o.earth_model_id = "%s/earth_model/%s" % (ID_ROOT, model)
        o.time = time

        # goto station and phases info lines
        while True:
            try:
                line = lines.pop(0)
            except:
                break
            if line.startswith(" STA NET COM L CR DIST AZM"):
                break

        oq.used_phase_count = 0
        oq.extra = AttribDict()
        oq.extra.usedPhaseCountP = {'value': 0, 'namespace': NAMESPACE}
        oq.extra.usedPhaseCountS = {'value': 0, 'namespace': NAMESPACE}
        used_stations = set()
        #XXX caution: we sometimes access the prior element!
        for i in range(len(lines)):
            # check which type of phase
            if lines[i][32] == "P":
                type = "P"
            elif lines[i][32] == "S":
                type = "S"
            else:
                continue
            # get values from line
            station = lines[i][0:6].strip()
            if station == "":
                station = sta_map_reverse[lines[i-1][0:6].strip()]
                distance = float(lines[i-1][18:23])
                azimuth = int(lines[i-1][23:26])
                #XXX TODO check, if incident is correct!!
                incident = int(lines[i-1][27:30])
            else:
                station = sta_map_reverse[station]
                distance = float(lines[i][18:23])
                azimuth = int(lines[i][23:26])
                #XXX TODO check, if incident is correct!!
                incident = int(lines[i][27:30])
            used_stations.add(station)
            if lines[i][31] == "I":
                onset = "impulsive"
            elif lines[i][31] == "E":
                onset = "emergent"
            else:
                onset = None
            if lines[i][33] == "U":
                polarity = "positive"
            elif lines[i][33] == "D":
                polarity = "negative"
            else:
                polarity = None
            res = float(lines[i][61:66])
            weight = float(lines[i][68:72])

            # assign synthetic phase info
            pick = self.getPick(station=station, phase_hint=type)
            if pick is None:
                msg = "This should not happen! Location output was read and a corresponding pick is missing!"
                warnings.warn(msg)
            arrival = Arrival(origin=o, pick=pick)
            # residual is defined as P-Psynth by NLLOC!
            # XXX does this also hold for hyp2000???
            arrival.time_residual = res
            arrival.azimuth = azimuth
            arrival.distance = kilometer2degrees(distance)
            arrival.takeoff_angle = incident
            if onset and not pick.onset:
                pick.onset = onset
            if polarity and not pick.polarity:
                pick.polarity = polarity
            # we use weights 0,1,2,3 but hypo2000 outputs floats...
            arrival.time_weight = weight
            o.quality.used_phase_count += 1
            if type == "P":
                o.quality.extra.usedPhaseCountP['value'] += 1
            elif type == "S":
                o.quality.extra.usedPhaseCountS['value'] += 1
            else:
                self.error("Phase '%s' not recognized as P or S. " % type +
                           "Not incrementing P nor S phase count.")
        o.used_station_count = len(used_stations)

    def updateMagnitude(self):
        if self.catalog[0].origins:
            self.info("updating magnitude info...")
            self.calculateStationMagnitudes()
            self.updateNetworkMag()
            # self.setXMLEventID()
        else:
            self.critical("can not update magnitude (no origin)...")

    def updateNetworkMag(self):
        self.info("updating network magnitude...")
        event = self.catalog[0]

        if not event.origins:
            event.magnitudes = []
            self.critical("no origin information for magnitude...")
            return

        origin = event.origins[0]

        used_stamags = []
        for sm in event.station_magnitudes:
            if sm.origin_id != origin.resource_id:
                msg = ("Skipping station magnitude with non-matching origin "
                       "ID (%s; Current origin: %s)" % (sm.origin_id,
                                                        origin.resource_id))
                self.error(msg)
                continue
            if not sm.get("used", True):
                msg = ("Skipping manually deselected station magnitude for "
                       "station %s." % sm.waveform_id.station_code)
                self.error(msg)
                continue
            used_stamags.append(sm)

        if not used_stamags:
            event.magnitudes = []
            self.critical("no station magnitudes (or all deselected), nothing to do...")
            return

        m = Magnitude()
        event.magnitudes = [m]
        m.method_id = "/".join([ID_ROOT, "magnitude_method", "obspyck", "2"])
        m.origin_id = origin.resource_id
        m.type = "ML"

        mag_values = [sm_.mag for sm_ in used_stamags]
        m.mag = np.mean(mag_values)
        m.mag_errors.uncertainty = np.std(mag_values)
        m.mag_errors.confidence_level = ONE_SIGMA
        m.station_count = len(mag_values)
        single_weights = 1.0 / len(mag_values)

        m.station_magnitude_contributions = []
        for sm in used_stamags:
            contrib = StationMagnitudeContribution()
            contrib.station_magnitude_id = sm.resource_id
            contrib.weight = single_weights
            m.station_magnitude_contributions.append(contrib)

        self.critical("new network magnitude: %.2f (Std: %.2f)" % (
            m.mag, m.mag_errors.uncertainty))

        # XXX TODO: do we need to updat colors here?
        if self.widgets.qToolButton_showMap.isChecked():
            return

    # XXX TODO Hypo distances needed?? where??
    def calculateEpiHypoDists(self):
        o = self.catalog[0].origins[0]
        if not o.longitude or not o.latitude:
            err = "Error: No coordinates for origin!"
            self.error(err)
        # XXX TODO need to check that distances are stored with arrival upon
        # creation
        epidists = [a.distance for a in o.arrivals]
        if not o.quality:
            o.quality = OriginQuality()
        o.quality.maximum_distance = max(epidists)
        o.quality.minimum_distance = min(epidists)
        o.quality.median_distance = np.median(epidists)

    def epidist_for_stream(self, stream):
        """
        Return epicentral distance for given stream (in kilometers), return
        None if epicentral distance can not be computed (no origin, no
        station coordinates, ..).
        """
        if not self.catalog or not self.catalog[0].origins:
            err = 'Can not compute epicentral distance, no origin information.'
            self.error(err)
            return None
        o = self.catalog[0].origins[0]
        if not o.longitude or not o.latitude:
            err = ('Can not compute epicentral distance, origin is missing '
                   'latitude and/or longitude.')
            self.error(err)
            return None
        tr = stream[0]
        try:
            sta_lon = tr.stats.coordinates['longitude']
            sta_lat = tr.stats.coordinates['latitude']
        except:
            err = ('Can not compute epicentral distance, stream ({}) metadata '
                   'is missing latitude and/or longitude.').format(tr.id)
            self.error(err)
            return None
        epi_dist, _, _ = gps2dist_azimuth(o.latitude, o.longitude,
                                          sta_lat, sta_lon)
        return epi_dist / 1e3

    def hypoDist(self, coords):
        o = self.catalog[0].origins[0]
        epi_dist, _, _ = gps2dist_azimuth(o.latitude, o.longitude,
                                          coords['latitude'],
                                          coords['longitude'])
        # origin depth is in m positive down,
        # station elevation is in m positive up
        if abs(o.depth) < 800:
            msg = ("Calculating hypocentral distance for origin "
                   "depth '%s' meters." % o.depth)
            self.error(msg)
            warnings.warn(msg)
        if abs(coords['elevation']) < 8:
            msg = ("Calculating hypocentral distance for station "
                   "elevation '%s' meters." % coords['elevation'])
            self.error(msg)
        # if sensor is buried or downhole, account for the specified sensor
        # depth
        z_dist = o.depth + coords['elevation'] - coords.get('local_depth', 0)
        return np.sqrt(epi_dist ** 2 + z_dist ** 2) / 1e3

    # XXX TODO maybe rename to "updateStationMagnitude"
    # XXX TODO automatically update magnitude on setting amplitude picks!
    def calculateStationMagnitudes(self):
        event = self.catalog[0]
        origin = event.origins[0]
        event.station_magnitudes = []

        netstaloc = set([(amp.waveform_id.network_code,
                          amp.waveform_id.station_code,
                          amp.waveform_id.location_code)
                         for amp in event.amplitudes])

        for net, sta, loc in netstaloc:
            amplitudes = []
            timedeltas = []
            pazs = []
            channels = []
            p2ps = []
            for amplitude in self.getAmplitudes(net, sta, loc):
                self.debug(str(amplitude))
                timedelta = amplitude.get_timedelta()
                self.debug("Timedelta: " + str(timedelta))
                if timedelta is None:
                    continue
                tr = self.getTrace(
                    seed_string=amplitude.waveform_id.get_seed_string())
                # either use attached PAZ or response..
                if "parser" in tr.stats:
                    paz = tr.stats["parser"].get_paz(tr.id, tr.stats.starttime)
                elif "response" in tr.stats:
                    paz = tr.stats["response"]
                else:
                    paz = None
                self.debug("PAZ/response: " + str(paz))
                if paz is None:
                    # XXX TODO we could fetch the metadata from seishub if we
                    # don't have a trace with PAZ and still use the stored info
                    msg = ("Skipping amplitude for station "
                           "'%s': Missing PAZ/response metadata" % sta)
                    self.error(msg)
                    continue
                amplitudes.append(amplitude)
                p2ps.append(amplitude.get_p2p())
                timedeltas.append(timedelta)
                pazs.append(paz)
                channels.append(tr.stats.channel)

            if not amplitudes:
                continue

            dist = self.hypoDist(tr.stats.coordinates)
            mag = estimate_magnitude(pazs, p2ps, timedeltas, dist)
            sm = StationMagnitude()
            event.station_magnitudes.append(sm)
            sm.origin_id = origin.resource_id
            sm.method_id = "/".join(
                [ID_ROOT, "station_magnitude_method", "obspyck", "2"])
            sm.mag = mag
            sm.type = "ML"
            sm.waveform_id = WaveformStreamID()
            sm.waveform_id.network_code = net
            sm.waveform_id.station_code = sta
            sm.waveform_id.location_code = loc
            extra = sm.setdefault("extra", AttribDict())
            extra.channels = {'value': ",".join(channels),
                              'namespace': NAMESPACE}
            extra.amplitudeIDs = {'value': ",".join([str(a.resource_id)
                                                     for a in amplitudes]),
                                  'namespace': NAMESPACE}
            self.critical('calculated new magnitude for %s: %0.2f (channels: %s)' % (
                sta, mag, ",".join(channels)))

    #see http://www.scipy.org/Cookbook/LinearRegression for alternative routine
    #XXX replace with drawWadati()
    def drawWadati(self):
        """
        Shows a Wadati diagram plotting P time in (truncated) Julian seconds
        against S-P time for every station and doing a linear regression
        using rpy. An estimate of Vp/Vs is given by the slope + 1.
        """
        try:
            import rpy
        except:
            err = "Error: Package rpy could not be imported!\n" + \
                  "(We should switch to scipy polyfit, anyway!)"
            self.error(err)
            return
        pTimes = []
        spTimes = []
        stations = []
        for st in self.streams:
            net = st[0].stats.network
            sta = st[0].stats.station
            pick_p = self.getPick(network=net, station=sta, phase_hint='P')
            pick_s = self.getPick(network=net, station=sta, phase_hint='S')
            if pick_p and pick_s:
                p = pick_p.time
                p = "%.3f" % p.timestamp
                p = float(p[-7:])
                pTimes.append(p)
                sp = pick_s.time - pick_p.time
                spTimes.append(sp)
                stations.append(st[0].stats.station)
            else:
                continue
        if len(pTimes) < 2:
            err = "Error: Less than 2 P-S Pairs!"
            self.error(err)
            return
        my_lsfit = rpy.r.lsfit(pTimes, spTimes)
        gradient = my_lsfit['coefficients']['X']
        intercept = my_lsfit['coefficients']['Intercept']
        vpvs = gradient + 1.
        ressqrsum = 0.
        for res in my_lsfit['residuals']:
            ressqrsum += (res ** 2)
        y0 = 0.
        x0 = - (intercept / gradient)
        x1 = max(pTimes)
        y1 = (gradient * float(x1)) + intercept

        fig = self.fig
        self.axWadati = fig.add_subplot(111)
        self.fig.subplots_adjust(bottom=0.07, top=0.95, left=0.07, right=0.98)
        ax = self.axWadati
        ax = fig.add_subplot(111)

        ax.scatter(pTimes, spTimes)
        for i, station in enumerate(stations):
            ax.text(pTimes[i], spTimes[i], station, va = "top")
        ax.plot([x0, x1], [y0, y1])
        ax.axhline(0, color="blue", ls=":")
        # origin time estimated by wadati plot
        ax.axvline(x0, color="blue", ls=":",
                   label="origin time from wadati diagram")
        # origin time from event location
        if self.catalog[0].origins[0].time:
            otime = "%.3f" % self.catalog[0].origins[0].time.timestamp
            otime = float(otime[-7:])
            ax.axvline(otime, color="red", ls=":",
                       label="origin time from event location")
        ax.text(0.1, 0.7, "Vp/Vs: %.2f\nSum of squared residuals: %.3f" % \
                (vpvs, ressqrsum), transform=ax.transAxes)
        ax.text(0.1, 0.1, "Origin time from event location", color="red",
                transform=ax.transAxes)
        #ax.axis("auto")
        ax.set_xlim(min(x0 - 1, otime - 1), max(pTimes) + 1)
        ax.set_ylim(-1, max(spTimes) + 1)
        ax.set_xlabel("absolute P times (julian seconds, truncated)")
        ax.set_ylabel("P-S times (seconds)")
        ax.set_title("Wadati Diagram")
        self.canv.draw()

    def delWadati(self):
        if hasattr(self, "axWadati"):
            self.fig.delaxes(self.axWadati)
            del self.axWadati

    def drawStreamOverview(self):
        event = self.catalog[0]
        stNum = len(self.streams)
        fig = self.fig
        axs = []
        self.axs = axs
        plts = []
        self.plts = plts
        trans = []
        self.trans = trans
        t = []
        alphas = {'Z': 1.0, 'L': 1.0, '1': 1.0,
                  'N': 0.8, 'Q': 0.8, 'R': 0.8, 'E': 0.8, 'T': 0.8}
        for i, st in enumerate(self.streams_bkp):
            st = st.copy()
            for j, tr in enumerate(st):
                net, sta, loc, cha = tr.id.split(".")
                color = COMPONENT_COLORS.get(cha[-1], "gray")
                alpha = alphas.get(cha[-1], 0.4)
                # make sure that the relative x-axis times start with 0 at the time
                # specified as start time on command line
                starttime_relative = self.time_abs2rel(tr.stats.starttime)
                sampletimes = np.arange(starttime_relative,
                        starttime_relative + (tr.stats.delta * tr.stats.npts),
                        tr.stats.delta)
                # sometimes our arange is one item too long (why??), so we just cut
                # off the last item if this is the case
                if len(sampletimes) == tr.stats.npts + 1:
                    sampletimes = sampletimes[:-1]
                t.append(sampletimes)
                if i == 0:
                    ax = fig.add_subplot(stNum, 1, i+1)
                else:
                    ax = fig.add_subplot(stNum, 1, i+1, sharex=axs[0], sharey=axs[0])
                    ax.xaxis.set_ticks_position("top")
                # only add axes for first trace in each stream to avoid
                # duplicates
                if j == 0:
                    axs.append(ax)
                    trans.append(mpl.transforms.blended_transform_factory(ax.transData, ax.transAxes))
                ax.xaxis.set_major_formatter(FuncFormatter(formatXTicklabels))
                # normalize with overall sensitivity and convert to nm/s
                # if not explicitly deactivated on command line
                if self.config.getboolean("base", "normalization") and not self.config.getboolean("base", "no_metadata"):
                    if self.widgets.qToolButton_physical_units.isChecked():
                        self._physical_units(tr)
                    if self.widgets.qToolButton_filter.isChecked():
                        self._filter(tr)
                    data_ = tr.data
                    # try:
                    #     sensitivity = tr.stats.paz.sensitivity
                    # except AttributeError:
                    #     sensitivity = tr.stats.response.instrument_sensitivity.value
                    # data_ = tr.data * 1e9 / sensitivity
                    scaling = None
                else:
                    scaling = 1.0
                    data_ = tr.data
                plts.append(ax.plot(sampletimes, data_, color=color, alpha=alpha,
                                    zorder=1000, linewidth=1.)[0])
            # plot picks and arrivals
            # seiscomp does not store location code with picks, so allow to
            # match any location code in that case..
            try:
                if event.get("creation_info", {}).get("author", "").startswith("scevent"):
                    loc = None
            except AttributeError: # If no creation_info
                pass
            picks = self.getPicks(network=net, station=sta)
            try:
                arrivals = event.origins[0].arrivals
            except:
                arrivals = []
            for pick in picks:
                if not pick.time:
                    continue
                arrival = getArrivalForPick(arrivals, pick)
                self.drawPick(ax, pick, main_axes=True)
                # don't draw pick labels because they totally clutter the
                # stream overview otherwise..
                # self.drawPickLabel(ax, pick)
                if arrival is not None:
                    self.drawArrival(ax, arrival, pick, main_axes=True)
            # plot amplitudes
            amplitudes = self.getAmplitudes(network=net, station=sta, location=loc)
            for amplitude in amplitudes:
                self.drawAmplitude(ax, amplitude, scaling=scaling)
        self.drawIds()
        axs[-1].xaxis.set_ticks_position("both")
        label = self.TREF.isoformat().replace("T", "  ")
        self.supTit = fig.suptitle(label, ha="left", va="bottom",
                                   x=0.01, y=0.01)
        self.xMin, self.xMax = axs[0].get_xlim()
        self.yMin, self.yMax = axs[0].get_ylim()
        fig.subplots_adjust(bottom=0.001, hspace=0.000, right=0.999, top=0.999, left=0.001)

    def drawEventMap(self):
        event = self.catalog[0]
        try:
            o = event.origins[0]
        except IndexError:
            err = "Error: No hypocenter data!"
            self.error(err)
            return
        #XXX self.figEventMap.canvas.widgetlock.release(toolbar)
        #self.axEventMap = self.fig.add_subplot(111)
        bbox = mpl.transforms.Bbox.from_extents(0.08, 0.08, 0.92, 0.92)
        self.axEventMap = self.fig.add_axes(bbox, aspect='equal', adjustable='datalim')
        axEM = self.axEventMap
        #axEM.set_aspect('equal', adjustable="datalim")
        #self.fig.subplots_adjust(bottom=0.07, top=0.95, left=0.07, right=0.98)
        axEM.scatter([o.longitude], [o.latitude], 30, color='red', marker='o')
        # XXX TODO handle different origin uncertainty descriptions
        #errLon, errLat = util_lon_lat(o.longitude, o.latitude, o.longitude_errors,
        #                              o.latitude_errors)

        ou = o.origin_uncertainty
        errX, errY = None, None
        if ou is not None:
            # azimuth measured from north gives direction of ellipse major axis
            if ou.azimuth_max_horizontal_uncertainty == 0:
                errX, errY = (ou.min_horizontal_uncertainty,
                              ou.max_horizontal_uncertainty)
            elif ou.azimuth_max_horizontal_uncertainty == 90:
                errX, errY = (ou.max_horizontal_uncertainty,
                              ou.min_horizontal_uncertainty)
            else:
                # XXX TODO: support any error ellipses
                msg = ("Error ellipses with azimuth not equal to 0 or 90 "
                       "degrees from North are not supported yet..")
                self.error(msg)
            if errX and errY:
                errLon, errLat = util_lon_lat(o.longitude, o.latitude,
                                              errX / 1e3, errY / 1e3)
                errLon -= o.longitude
                errLat -= o.latitude
                if ou.preferred_description == "uncertainty ellipse":
                    errorell = Ellipse(xy=[o.longitude, o.latitude],
                                       width=errLon,
                                       height=errLat,
                                       # we account for angle by setting errX/Y
                                       #angle=ou.azimuth_max_horizontal_uncertainty,
                                       fill=False)
                    axEM.add_artist(errorell)
        m = event.magnitudes and event.magnitudes[0] or None
        try:
            self.critical("%s %.2f %.6f %.6f %.3f %.3f %.3f %.4f %.6f" % (
                o.time, m.mag, o.longitude, o.latitude, o.depth / 1e3,
                errX / 1e3, errY / 1e3,
                o.depth_errors.uncertainty / 1e3, o.quality.standard_error))
        except:
            pass
        ypos = 0.97
        xpos = 0.03
        info = ["Origin:",
                " Time: %s" % o.time.strftime("%Y-%m-%d %H:%M:%S UTC (%a)")]
        if errX is not None:
            info.append(" Longitude: %.5f +/- %0.2fkm" % (o.longitude, errX / 1e3))
            info.append(" Latitude: %.5f +/- %0.2fkm" % (o.latitude, errY / 1e3))
        else:
            info.append(" Longitude: %.5f" % o.longitude)
            info.append(" Latitude: %.5f" % o.latitude)
        info.append(" Depth: %.3f km +/- %0.2fkm" % (o.depth / 1e3, o.depth_errors.uncertainty / 1e3))
        if o.quality and o.quality.standard_error:
            info.append(" RMS: %.5f" % o.quality.standard_error)
        else:
            info.append(" RMS: ---")
        if m is not None:
            m_ = m.mag
            m_err_ = m.mag_errors.uncertainty
            info.append("")
            info.append("Magnitude: %.1f +/- %.2f" % (m_, m_err_))
        info = "\n".join(info)
        axEM.text(xpos, ypos, info, va='top', ha='left', family='monospace',
                  transform=axEM.transAxes)
        #if o.quality and o.quality.standard_error:
        #    axEM.text(xpos, ypos, "\n\n\n\n Residual: %.3f s" % \
        #              o.quality.standard_error, va='top', ha='left',
        #              color=PHASE_COLORS['P'], transform=axEM.transAxes,
        #              family='monospace')
        link = "http://maps.google.de/maps?f=q&q=%.6f,%.6f" % \
               (o.latitude, o.longitude)
        self.widgets.qPlainTextEdit_stdout.appendHtml("<a href='%s'>%s</a> &nbsp;" % (link, link))
        self.scatterMagIndices = []
        self.scatterMagLon = []
        self.scatterMagLat = []
        self.scatterMagUsed = []
        # XXX TODO: this plotting should be based on the contents of the
        # catalog and not on the waveform data
        used_stamags = []
        for i, st in enumerate(self.streams):
            # determine which stations are used in location, set color
            net = st[0].stats.network
            sta = st[0].stats.station
            coords = st[0].stats.coordinates
            pick_p = self.getPick(network=net, station=sta, phase_hint='P')
            pick_s = self.getPick(network=net, station=sta, phase_hint='S')
            arrival_p = pick_p and getArrivalForPick(o.arrivals, pick_p)
            arrival_s = pick_s and getArrivalForPick(o.arrivals, pick_s)
            if ((arrival_p and arrival_p.time_residual is not None) or
                    (arrival_s and arrival_s.time_residual is not None)):
                stationColor = 'black'
            else:
                stationColor = 'gray'
            # plot stations at respective coordinates with names
            axEM.scatter((coords.longitude,), (coords.latitude,), s=300,
                         marker='v', color='', edgecolor=stationColor)
            axEM.text(coords.longitude, coords.latitude, '  ' + sta,
                      color=stationColor, va='top', family='monospace')
            for _i, (pick, arrival) in enumerate([[pick_p, arrival_p], [pick_s, arrival_s]]):
                if not (pick and arrival):
                    continue
                if arrival.time_residual is not None:
                    res_info = '\n' * (_i + 2) + '%+0.3fs' % arrival.time_residual
                    if pick.polarity:
                        res_info += '  %s' % pick.polarity
                    axEM.text(coords.longitude, coords.latitude, res_info,
                              va='top', family='monospace',
                              color=self.seismic_phases[pick.phase_hint])
            for sm in self.catalog[0].station_magnitudes:
                if sm.waveform_id.station_code != sta:
                    continue
                used_stamags.append(sm)
                self.scatterMagIndices.append(i)
                self.scatterMagLon.append(coords.longitude)
                self.scatterMagLat.append(coords.latitude)
                self.scatterMagUsed.append(sm.get("used", True))
                try:
                    chann_info = " (%s)" % sm.extra.channels["value"]
                except:
                    chann_info = ""
                label = '\n' * (_i + 3) + \
                        '  %0.2f%s' % (sm.mag, chann_info)
                axEM.text(coords.longitude, coords.latitude, label, va='top',
                          family='monospace', color=self._magnitude_color)
                break

        if len(self.scatterMagLon) > 0:
            self.scatterMag = axEM.scatter(self.scatterMagLon,
                    self.scatterMagLat, s=150, marker='v', color='',
                    edgecolor='black', picker=10)

        axEM.set_xlabel('Longitude')
        axEM.set_ylabel('Latitude')
        time = o.time
        timestr = time.strftime("%Y-%m-%d  %H:%M:%S")
        timestr += ".%02d" % (time.microsecond / 1e4 + 0.5)
        axEM.set_title(timestr)
        # save id to disconnect when switching back to stream dislay
        self.eventMapPickEvent = self.canv.mpl_connect('pick_event',
                                                       self.selectMagnitudes)
        try:
            colors = [x is True and (0, 1, 0, 1) or (0, 0, 0, 0)
                      for x in [sm.used for sm in used_stamags]]
            self.scatterMag.set_facecolors(colors)
        except:
            pass

        # make hexbin scatter plot, if located with NLLoc
        # XXX no vital commands should come after this block, as we do not
        # handle exceptions!
        data = o.get("nonlinloc_scatter")
        if data is not None:
            data = data.T
            cmap = mpl.cm.gist_heat_r
            axEM.hexbin(data[0], data[1], cmap=cmap, zorder=-1000)

            self.axEventMapInletXY = self.fig.add_axes([0.8, 0.8, 0.16, 0.16])
            axEMiXY = self.axEventMapInletXY
            self.axEventMapInletXZ = self.fig.add_axes([0.8, 0.73, 0.16, 0.06],
                    sharex=axEMiXY)
            self.axEventMapInletZY = self.fig.add_axes([0.73, 0.8, 0.06, 0.16],
                    sharey=axEMiXY)
            axEMiXZ = self.axEventMapInletXZ
            axEMiZY = self.axEventMapInletZY

            # z axis in km
            axEMiXY.hexbin(data[0], data[1], cmap=cmap)
            axEMiXZ.hexbin(data[0], data[2], cmap=cmap)
            axEMiZY.hexbin(data[2], data[1], cmap=cmap)
            stalons = [st[0].stats.coordinates.longitude for st in self.streams]
            stalats = [st[0].stats.coordinates.latitude for st in self.streams]
            stadepths = []
            for st in self.streams:
                coords_ = st[0].stats.coordinates
                elev_ = coords_.elevation
                # if sensor is buried or downhole, account for the specified
                # sensor depth
                depth_ = coords_.get('local_depth')
                if depth_:
                    elev_ -= depth_
                stadepths.append(elev_)
            axEMiXY.scatter(stalons, stalats, s=200, marker='v', color='k')
            axEMiXZ.scatter(stalons, stadepths, s=200, marker='v', color='k')
            axEMiZY.scatter(stadepths, stalats, s=200, marker='v', color='k')

            min_x = min(data[0])
            max_x = max(data[0])
            min_y = min(data[1])
            max_y = max(data[1])
            min_z = min(data[2])
            max_z = max(data[2])
            axEMiZY.set_xlim(min_z, max_z)
            axEMiXZ.set_ylim(min_z, max_z)
            axEMiXY.set_xlim(min_x, max_x)
            axEMiXY.set_ylim(min_y, max_y)
            axEMiXZ.invert_yaxis()
            axEMiZY.invert_xaxis()

            formatter = FormatStrFormatter("%.3f")
            axEMiXY.xaxis.set_major_formatter(formatter)
            axEMiXY.yaxis.set_major_formatter(formatter)

            # only draw very few ticklabels in our tiny subaxes
            for ax in [axEMiXZ.xaxis, axEMiXZ.yaxis,
                       axEMiZY.xaxis, axEMiZY.yaxis]:
                ax.set_major_locator(MaxNLocator(nbins=3))

            # hide ticklabels on XY plot
            for ax in [axEMiXY.xaxis, axEMiXY.yaxis]:
                plt.setp(ax.get_ticklabels(), visible=False)


    def delEventMap(self):
        try:
            self.canv.mpl_disconnect(self.eventMapPickEvent)
        except AttributeError:
            pass
        if hasattr(self, "axEventMapInletXY"):
            self.fig.delaxes(self.axEventMapInletXY)
            del self.axEventMapInletXY
        if hasattr(self, "axEventMapInletXZ"):
            self.fig.delaxes(self.axEventMapInletXZ)
            del self.axEventMapInletXZ
        if hasattr(self, "axEventMapInletZY"):
            self.fig.delaxes(self.axEventMapInletZY)
            del self.axEventMapInletZY
        if hasattr(self, "axEventMap"):
            self.fig.delaxes(self.axEventMap)
            del self.axEventMap

    def selectMagnitudes(self, event):
        if not self.widgets.qToolButton_showMap.isChecked():
            return
        if event.artist != self.scatterMag:
            return
        j = event.ind[0]
        i = self.scatterMagIndices[j]
        net = self.streams[i][0].stats.network
        sta = self.streams[i][0].stats.station
        loc = self.streams[i][0].stats.location
        stamag = self.getStationMagnitude(
            network=net, station=sta, location=loc)
        if stamag is None:
            return
        stamag.used = not stamag.used
        colors = self.scatterMag.get_facecolors()
        colors[j] = stamag.used and (0, 1, 0, 1) or (0, 0, 0, 0)
        self.scatterMag.set_facecolors(colors)
        self.updateNetworkMag()
        self.canv.draw()

    # XXX TODO rename method
    def dicts2hypo71Stations(self):
        """
        Returns the station location information in hypo71
        stations file format as a string. This string can then be written to
        a file.
        """
        sta_map = self._4_letter_sta_map
        fmt = "%6s%02i%05.2f%1s%03i%05.2f%1s%4i\n"
        hypo71_string = ""

        for st in self.streams:
            stats = st[0].stats
            sta = stats.station
            lon = stats.coordinates.longitude
            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - abs(lon_deg)) * 60.
            lat = stats.coordinates.latitude
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - abs(lat_deg)) * 60.
            hem_NS = 'N'
            hem_EW = 'E'
            if lat < 0:
                hem_NS = 'S'
            if lon < 0:
                hem_EW = 'W'
            # hypo 71 format uses elevation in meters not kilometers
            ele = stats.coordinates.elevation
            # if sensor is buried or downhole, account for the specified sensor
            # depth
            depth = stats.coordinates.get('local_depth')
            if depth:
                ele -= depth
            hypo71_string += fmt % (sta_map[sta], lat_deg, lat_min, hem_NS,
                                    lon_deg, lon_min, hem_EW, ele)

        return hypo71_string

    def dicts2hypo71Phases(self):
        """
        Returns the pick information in hypo71 phase file format
        as a string. This string can then be written to a file.

        Information on the file formats can be found at:
        http://geopubs.wr.usgs.gov/open-file/of02-171/of02-171.pdf p.30

        Quote:
        The traditional USGS phase data input format (not Y2000 compatible)
        Some fields were added after the original HYPO71 phase format
        definition.

        Col. Len. Format Data
         1    4  A4       4-letter station site code. Also see col 78.
         5    2  A2       P remark such as "IP". If blank, any P time is
                          ignored.
         7    1  A1       P first motion such as U, D, +, -, C, D.
         8    1  I1       Assigned P weight code.
         9    1  A1       Optional 1-letter station component.
        10   10  5I2      Year, month, day, hour and minute.
        20    5  F5.2     Second of P arrival.
        25    1  1X       Presently unused.
        26    6  6X       Reserved remark field. This field is not copied to
                          output files.
        32    5  F5.2     Second of S arrival. The S time will be used if this
                          field is nonblank.
        37    2  A2, 1X   S remark such as "ES".
        40    1  I1       Assigned weight code for S.
        41    1  A1, 3X   Data source code. This is copied to the archive
                          output.
        45    3  F3.0     Peak-to-peak amplitude in mm on Develocorder viewer
                          screen or paper record.
        48    3  F3.2     Optional period in seconds of amplitude read on the
                          seismogram. If blank, use the standard period from
                          station file.
        51    1  I1       Amplitude magnitude weight code. Same codes as P & S.
        52    3  3X       Amplitude magnitude remark (presently unused).
        55    4  I4       Optional event sequence or ID number. This number may
                          be replaced by an ID number on the terminator line.
        59    4  F4.1     Optional calibration factor to use for amplitude
                          magnitudes. If blank, the standard cal factor from
                          the station file is used.
        63    3  A3       Optional event remark. Certain event remarks are
                          translated into 1-letter codes to save in output.
        66    5  F5.2     Clock correction to be added to both P and S times.
        71    1  A1       Station seismogram remark. Unused except as a label
                          on output.
        72    4  F4.0     Coda duration in seconds.
        76    1  I1       Duration magnitude weight code. Same codes as P & S.
        77    1  1X       Reserved.
        78    1  A1       Optional 5th letter of station site code.
        79    3  A3       Station component code.
        82    2  A2       Station network code.
        84-85 2  A2     2-letter station location code (component extension).
        """
        sta_map = self._4_letter_sta_map

        fmtP = "%4s%1sP%1s%1i %15s"
        fmtS = "%12s%1sS%1s%1i\n"
        hypo71_string = ""

        for st in self.streams:
            net = st[0].stats.network
            sta = st[0].stats.station
            pick_p = self.getPick(network=net, station=sta, phase_hint='P')
            pick_s = self.getPick(network=net, station=sta, phase_hint='S')
            if not pick_p and not pick_s:
                continue
            if not pick_p:
                msg = ("Hypo2000 phase file format does not support S pick "
                       "without P pick. Skipping station: %s") % sta
                self.error(msg)
                continue

            # P Pick
            pick = pick_p
            t = pick.time
            hundredth = int(round(t.microsecond / 1e4))
            if hundredth == 100:  # XXX check!!
                t_p = t + 1
                hundredth = 0
            else:
                t_p = t
            date = t_p.strftime("%y%m%d%H%M%S") + ".%02d" % hundredth
            if pick.onset == 'impulsive':
                onset = 'I'
            elif pick.onset == 'emergent':
                onset = 'E'
            else:
                onset = '?'
            if pick.polarity == "positive":
                polarity = "U"
            elif pick.polarity == "negative":
                polarity = "D"
            else:
                polarity = "?"
            try:
                weight = int(pick.extra.weight.value)
            except:
                weight = 0
            hypo71_string += fmtP % (sta_map[sta], onset, polarity, weight, date)

            # S Pick
            if pick_s:
                if not pick_p:
                    err = "Warning: Trying to print a Hypo2000 phase file " + \
                          "with an S phase without P phase.\n" + \
                          "This case might not be covered correctly and " + \
                          "could screw our file up!"
                    self.error(err)
                pick = pick_s
                t2 = pick.time
                # if the S time's absolute minute is higher than that of the
                # P pick, we have to add 60 to the S second count for the
                # hypo 2000 output file
                # +60 %60 is necessary if t.min = 57, t2.min = 2 e.g.
                mindiff = (t2.minute - t.minute + 60) % 60
                abs_sec = t2.second + (mindiff * 60)
                if abs_sec > 99:
                    err = "Warning: S phase seconds are greater than 99 " + \
                          "which is not covered by the hypo phase file " + \
                          "format! Omitting S phase of station %s!" % sta
                    self.error(err)
                    hypo71_string += "\n"
                    continue
                hundredth = int(round(t2.microsecond / 1e4))
                if hundredth == 100:
                    abs_sec += 1
                    hundredth = 0
                date2 = "%s.%02d" % (abs_sec, hundredth)
                if pick.onset == 'impulsive':
                    onset2 = 'I'
                elif pick.onset == 'emergent':
                    onset2 = 'E'
                else:
                    onset2 = '?'
                if pick.polarity == "positive":
                    polarity2 = "U"
                elif pick.polarity == "negative":
                    polarity2 = "D"
                else:
                    polarity2 = "?"
                try:
                    weight2 = int(pick.extra.weight.value)
                except:
                    weight2 = 0
                hypo71_string += fmtS % (date2, onset2, polarity2, weight2)
            else:
                hypo71_string += "\n"

        return hypo71_string

    def dicts2NLLocPhases(self):
        """
        Returns the pick information in NonLinLoc's own phase
        file format as a string. This string can then be written to a file.
        Currently only those fields really needed in location are actually used
        in assembling the phase information string.

        Information on the file formats can be found at:
        http://alomax.free.fr/nlloc/soft6.00/formats.html#_phase_

        Quote:
        NonLinLoc Phase file format (ASCII, NLLoc obsFileType = NLLOC_OBS)

        The NonLinLoc Phase file format is intended to give a comprehensive
        phase time-pick description that is easy to write and read.

        For each event to be located, this file contains one set of records. In
        each set there is one "arrival-time" record for each phase at each seismic
        station. The final record of each set is a blank. As many events as desired can
        be included in one file.

        Each record has a fixed format, with a blank space between fields. A
        field should never be left blank - use a "?" for unused characther fields and a
        zero or invalid numeric value for numeric fields.

        The NonLinLoc Phase file record is identical to the first part of each
        phase record in the NLLoc Hypocenter-Phase file output by the program NLLoc.
        Thus the phase list output by NLLoc can be used without modification as time
        pick observations for other runs of NLLoc.

        NonLinLoc phase record:
        Fields:
        Station name (char*6)
            station name or code
        Instrument (char*4)
            instument identification for the trace for which the time pick
            corresponds (i.e. SP, BRB, VBB)
        Component (char*4)
            component identification for the trace for which the time pick
            corresponds (i.e. Z, N, E, H)
        P phase onset (char*1)
            description of P phase arrival onset; i, e
        Phase descriptor (char*6)
            Phase identification (i.e. P, S, PmP)
        First Motion (char*1)
            first motion direction of P arrival; c, C, u, U = compression;
            d, D = dilatation; +, -, Z, N; . or ? = not readable.
        Date (yyyymmdd) (int*6)
            year (with century), month, day
        Hour/minute (hhmm) (int*4)
            Hour, min
        Seconds (float*7.4)
            seconds of phase arrival
        Err (char*3)
            Error/uncertainty type; GAU
        ErrMag (expFloat*9.2)
            Error/uncertainty magnitude in seconds
        Coda duration (expFloat*9.2)
            coda duration reading
        Amplitude (expFloat*9.2)
            Maxumim peak-to-peak amplitude
        Period (expFloat*9.2)
            Period of amplitude reading
        PriorWt (expFloat*9.2)

        A-priori phase weight Currently can be 0 (do not use reading) or
        1 (use reading). (NLL_FORMAT_VER_2 - WARNING: under development)

        Example:

        GRX    ?    ?    ? P      U 19940217 2216   44.9200 GAU  2.00e-02 -1.00e+00 -1.00e+00 -1.00e+00
        GRX    ?    ?    ? S      ? 19940217 2216   48.6900 GAU  4.00e-02 -1.00e+00 -1.00e+00 -1.00e+00
        CAD    ?    ?    ? P      D 19940217 2216   46.3500 GAU  2.00e-02 -1.00e+00 -1.00e+00 -1.00e+00
        CAD    ?    ?    ? S      ? 19940217 2216   50.4000 GAU  4.00e-02 -1.00e+00 -1.00e+00 -1.00e+00
        BMT    ?    ?    ? P      U 19940217 2216   47.3500 GAU  2.00e-02 -1.00e+00 -1.00e+00 -1.00e+00
        """
        nlloc_str = ""

        for pick in self.catalog[0].picks:
            sta = pick.waveform_id.station_code.ljust(6)
            inst = "?".ljust(4)
            comp = "?".ljust(4)
            onset = "?"
            phase = pick.phase_hint.ljust(6)
            pol = "?"
            t = pick.time
            # CJH Hack to accommodate full microsecond precision...
            t = datetime.fromtimestamp(t.datetime.timestamp() * 100)
            date = t.strftime("%Y%m%d")
            hour_min = t.strftime("%H%M")
            sec = "%7.4f" % (t.second + t.microsecond / 1e6)
            error_type = "GAU"
            error = None
            # XXX check: should we take only half of the complete left-to-right error?!?
            if pick.time_errors.upper_uncertainty and pick.time_errors.lower_uncertainty:
                error = pick.time_errors.upper_uncertainty + pick.time_errors.lower_uncertainty
            elif pick.time_errors.uncertainty:
                error = 2 * pick.time_errors.uncertainty
            if error is None:
                error = self.config.getfloat("nonlinloc",
                                             "default_pick_uncertainty")
                err = ("Warning: Missing pick error. Using a default error "
                       "of {}s for {} phase of station {}. Please set pick "
                       "errors.").format(error, phase.strip(), sta.strip())
                self.error(err)
            error = "%9.2e" % error
            coda_dur = "-1.00e+00"
            ampl = "-1.00e+00"
            period = "-1.00e+00"
            fields = [sta, inst, comp, onset, phase, pol, date, hour_min,
                      sec, error_type, error, coda_dur, ampl, period]
            phase_str = " ".join(fields)
            nlloc_str += phase_str + "\n"
        return nlloc_str

    def get_QUAKEML_string(self):
        """
        Returns all information as xml file (type string)
        """
        cat = self.catalog
        cat.creation_info.creation_time = UTCDateTime()
        cat.creation_info.version = VERSION_INFO
        e = cat[0]
        extra = e.setdefault("extra", AttribDict())
        public = self.widgets.qCheckBox_public.isChecked()

        extra.evaluationMode = {'value': "manual", 'namespace': NAMESPACE}
        extra.public = {'value': public, 'namespace': NAMESPACE}

        # check if an quakeML event type should be set
        event_quakeml_type = str(self.widgets.qComboBox_eventType.currentText())
        if event_quakeml_type != '<event type>':
            e.event_type = event_quakeml_type

        # XXX TODO change handling of resource ids. never change id set at
        # creation and make sure that only wanted arrivals/picks get
        # saved/stored.

        with NamedTemporaryFile() as tf:
            tmp = tf.name
            cat.write(tmp, "QUAKEML", nsmap=NSMAP)
            with open(tmp) as fh:
                xml = fh.read()

        return xml

    def setXMLEventID(self, event_id=None):
        #XXX TODO: is problematic if two people create an event at exactly the same second!
        # then one event is overwritten with the other during submission.
        if event_id is None:
            event_id = UTCDateTime().strftime('%Y%m%d%H%M%S')
        self.catalog[0].resource_id = "/".join([ID_ROOT, "event", event_id])
        self.catalog.resource_id = "/".join([ID_ROOT, "catalog", event_id])

    def save_event_locally(self):
        """
        Save event locally as QuakeML file
        """
        # if we did no location at all, and only picks would be saved the
        # EventID ist still not set, so we have to do this now.
        if not self.catalog[0].get("resource_id"):
            err = "Error: Event resource_id not set."
            self.error(err)
            return
        name = str(self.catalog[0].resource_id).split("/")[-1] #XXX id of the file
        # create XML and also save in temporary directory for inspection purposes
        name = "obspyck_" + name
        if not name.endswith(".xml"):
            name += ".xml"
        self.info("creating xml...")
        data = self.get_QUAKEML_string()
        msg = "writing xml as %s"
        self.critical(msg % name)
        open(name, "wt").write(data)

    def upload_event(self):
        """
        Upload event to SeisHub and/or Jane
        """
        if not self.event_server:
            msg = "'event_server' not set in config, section [base]"
            self.error(msg)
            return

        # if we did no location at all, and only picks would be saved the
        # EventID ist still not set, so we have to do this now.
        if not self.catalog[0].get("resource_id"):
            err = "Error: Event resource_id not set."
            self.error(err)
            return

        name = str(self.catalog[0].resource_id).split("/")[-1] #XXX id of the file
        # create XML and also save in temporary directory for inspection purposes
        name = "obspyck_" + name
        if not name.endswith(".xml"):
            name += ".xml"
        tmpfile = os.path.join(self.tmp_dir, name)
        tmpfile2 = os.path.join(tempfile.gettempdir(), name)
        self.info("creating xml...")
        data = self.get_QUAKEML_string()
        self.update_qml_text(data)
        msg = "writing xml as %s and %s (for debugging purposes and in " + \
              "case of upload errors)"
        self.critical(msg % (tmpfile, tmpfile2))
        for fname in [tmpfile, tmpfile2]:
            open(fname, "wt").write(data)

        if self.event_server_type == "seishub":
            self.uploadSeisHub(name, data)
        elif self.event_server_type == "jane":
            conf = self.config
            server_key = conf.get("base", "event_server")
            client = self.event_server
            base_url = client.base_url
            user = conf.get(server_key, "user")
            password = conf.get(server_key, "password")
            self.upload_event_jane(name, data, base_url, user, password)
        else:
            raise ValueError()
        # XXX for transition to Jane, temporarily do both
        if self.test_event_server:
            self.upload_event_jane_test(name, data)

    def upload_event_jane_test(self, name, data):
        """
        Should be deleted again.. when Jane upload is properly tested and we
        drop parallel upload to Seishub+Jane again
        """
        conf = self.config
        server_key = conf.get("base", "test_event_server_jane")
        client = self.test_event_server
        base_url = client.base_url
        user = conf.get(server_key, "user")
        password = conf.get(server_key, "password")
        self.upload_event_jane(name, data, base_url, user, password)

    def upload_event_jane(self, name, data, base_url, user, password):
        url = base_url + "/rest/documents/quakeml/%s" % name
        r = requests.put(url=url, data=data, auth=(user, password))
        if not r.ok:
            msg = 'Something went wrong during upload to JANE! ({!s})'.format(
                r.status_code)
            self.error(msg)
            return

        msg = "Uploading Event!"
        msg += "\nJane Account: %s" % user
        msg += "\nAuthor: %s" % self.username
        msg += "\nName: %s" % name
        msg += "\nJane Server: %s" % base_url
        msg += "\nResponse: %s %s" % (r.status_code, r.text)
        self.critical(msg)

        if not self.catalog[0].origins:
            return

        origin = self.catalog[0].origins[0]
        nlloc_scatter = origin.get("nonlinloc_scatter")
        if nlloc_scatter is not None:
            sio = StringIO()
            header = "\n".join([
                "NonLinLoc Scatter",
                "Origin ID: {}".format(str(origin.resource_id)),
                "Longitude, Latitude, Z (km below sea level), PDF value",
                ])
            np.savetxt(sio, nlloc_scatter, fmt="%.6f %.6f %.4f %.2f",
                       header=header)
            sio.seek(0)
            data = sio.read()
            sio.close()
            url = "{}/rest/documents/quakeml/{}?format=json".format(
                base_url, name)
            r = requests.get(url, auth=(user, password))
            if not r.ok:
                msg = ('Something went wrong during NonLinLoc scatter upload '
                       'to JANE! ({!s})').format(r.status_code)
                self.error(msg)
            jane_id = r.json()["indices"][0]["id"]
            url = "{}/rest/document_indices/quakeml/{}/attachments".format(
                base_url, jane_id)
            headers = {"content-type": "text/plain",
                       "category": "nonlinloc_scatter"}
            r = requests.post(url=url, auth=(user, password), headers=headers,
                              data=data)
            if not r.ok:
                msg = ('Something went wrong during NonLinLoc scatter upload '
                       'to JANE! ({!s})').format(r.status_code)
                self.error(msg)

        msg = "Uploaded NonLinLoc Scatter as attachment"
        msg += "\nResponse: %s %s" % (r.status_code, r.text)
        self.critical(msg)

    def uploadSeisHub(self, name, data):
        """
        Upload quakeml file to SeisHub
        """
        # check, if the event should be uploaded as sysop. in this case we use
        # the sysop client instance for the upload (and also set
        # user_account in the xml to "sysop").
        # the correctness of the sysop password is tested when checking the
        # sysop box and entering the password immediately.
        if self.widgets.qCheckBox_public.isChecked():
            seishub_account = "sysop"
            client = self.clients['__SeisHub-sysop__']
        else:
            seishub_account = "obspyck"
            client = self.event_server

        headers = {}
        try:
            host = socket.gethostname()
        except:
            host = "localhost"
        headers["Host"] = host
        headers["User-Agent"] = "obspyck"
        headers["Content-type"] = "text/xml; charset=\"UTF-8\""
        headers["Content-length"] = "%d" % len(data)
        # XXX TODO: Calculate real PGV?!
        code, message = client.event.put_resource(name, xml_string=data,
                                                  headers=headers)
        msg = "Seishub Account: %s" % seishub_account
        msg += "\nUser: %s" % self.username
        msg += "\nName: %s" % name
        msg += "\nServer: %s" % self.config.get("base", "event_server")
        msg += "\nResponse: %s %s" % (code, message)
        self.critical(msg)

    def delete_event(self, resource_name):
        """
        Delete event from SeisHub and/or Jane
        """
        if not self.event_server:
            msg = "'event_server' not set in config, section [base]"
            self.error(msg)
            return

        if self.event_server_type == "seishub":
            self.deleteEventInSeisHub(resource_name)
        elif self.event_server_type == "jane":
            conf = self.config
            server_key = conf.get("base", "event_server")
            client = self.event_server
            base_url = client.base_url
            user = conf.get(server_key, "user")
            password = conf.get(server_key, "password")
            self.delete_event_jane(resource_name, base_url, user, password)
        else:
            raise ValueError()
        # for transition to Jane, temporarily do both
        if self.test_event_server:
            self.delete_event_jane_test(resource_name)

    def delete_event_jane_test(self, resource_name):
        conf = self.config
        server_key = conf.get("base", "test_event_server_jane")
        client = self.test_event_server
        base_url = client.base_url
        user = conf.get(server_key, "user")
        password = conf.get(server_key, "password")
        self.delete_event_jane(resource_name, base_url, user, password)

    def delete_event_jane(self, resource_name, base_url, user, password):
        r = requests.delete(
            url=base_url + "/rest/documents/quakeml/%s" % resource_name,
            auth=(user, password))
        if not r.ok:
            msg = 'Something went wrong during deletion on JANE! ({!s})'.format(
                r.status_code)
            self.error(msg)
            return

        msg = "Deleting Event!"
        msg += "\nJane Account: %s" % user
        msg += "\nAuthor: %s" % self.username
        msg += "\nName: %s" % resource_name
        msg += "\nJane Server: %s" % base_url
        msg += "\nResponse: %s %s" % (r.status_code, r.text)
        self.critical(msg)

    def deleteEventInSeisHub(self, resource_name):
        """
        Delete xml file from SeisHub.
        (Move to SeisHubs trash folder if this option is activated)
        """
        # check, if the event should be deleted as sysop. in this case we
        # use the sysop client instance for the DELETE request.
        # sysop may delete resources from any user.
        # at the moment deleted resources go to SeisHubs trash folder (and can
        # easily be resubmitted using the http interface).
        # the correctness of the sysop password is tested when checking the
        # sysop box and entering the password immediately.
        if self.widgets.qCheckBox_public.isChecked():
            seishub_account = "sysop"
            client = self.clients['__SeisHub-sysop__']
        else:
            seishub_account = "obspyck"
            client = self.event_server

        headers = {}
        try:
            host = socket.gethostname()
        except:
            host = "localhost"
        headers["Host"] = host
        headers["User-Agent"] = "obspyck"
        code, message = client.event.delete_resource(str(resource_name),
                                                     headers=headers)
        msg = "Deleting Event!"
        msg += "\nSeishub Account: %s" % seishub_account
        msg += "\nUser: %s" % self.username
        msg += "\nName: %s" % resource_name
        msg += "\nServer: %s" % self.config.get("base", "event_server")
        msg += "\nResponse: %s %s" % (code, message)
        self.critical(msg)

    def clearEvent(self):
        self.info("Clearing previous event data.")
        self.catalog = Catalog()
        event = Event()
        event.set_creation_info_username(self.username)
        self.catalog.events = [event]

    def clearOriginMagnitude(self):
        self.info("Clearing previous origin and magnitude data.")
        self.catalog[0].origins = []
        self.catalog[0].magnitudes = []
        self.catalog[0].station_magnitudes = []

    def clearFocmec(self):
        self.info("Clearing previous focal mechanism data.")
        self.catalog[0].focal_mechanisms = []
        self.focMechCurrent = None

    def updateAllItems(self):
        st = self.getCurrentStream()
        event = self.catalog[0]
        ids = []
        net = st[0].stats.network
        sta = st[0].stats.station
        loc = st[0].stats.location
        xlims = [list(ax.get_xlim()) for ax in self.axs]
        ylims = [list(ax.get_ylim()) for ax in self.axs]
        for _i, ax in enumerate(self.axs):
            # first line is waveform, leave it
            ax.lines = ax.lines[:1]
            # first text is trace id, leave it
            ax.texts = ax.texts[:1]
            # all patches are related to picks/amplitudes right now, remove all
            ax.patches = []
            ids.append(st[_i].id)
        # plot picks and arrivals
        # seiscomp does not store location code with picks, so allow to
        # match any location code in that case..
        try:
            if event.get("creation_info", {}).get("author", "").startswith("scevent"):
                loc = None
        except AttributeError: # No creation_info
            pass
        picks = self.getPicks(network=net, station=sta)
        try:
            arrivals = event.origins[0].arrivals
        except:
            arrivals = []
        for pick in picks:
            if not pick.time:
                continue
            arrival = getArrivalForPick(arrivals, pick)
            # do drawing in all axes
            for _id, ax in zip(ids, self.axs):
                self.debug(str(pick))
                self.debug(str(_id))
                if pick.waveform_id.get_seed_string() == _id:
                    main_axes = True
                    self.drawPickLabel(ax, pick)
                else:
                    main_axes = False
                self.drawPick(ax, pick, main_axes=main_axes)
                if arrival is not None:
                    self.drawArrival(ax, arrival, pick, main_axes=main_axes)
            # if no pick label was drawn yet.. draw it
            for _id, ax in zip(ids, self.axs):
                if pick.waveform_id.get_seed_string() == _id:
                    break
            else:
                self.drawPickLabel(self.axs[-1], pick, main_axes=False)
        # plot amplitudes
        if self.widgets.qToolButton_spectrogram.isChecked():
            pass
        elif self.widgets.qToolButton_trigger.isChecked():
            pass
        else:
            amplitudes = self.getAmplitudes(network=net, station=sta,
                                            location=loc)
            for amplitude in amplitudes:
                if amplitude is None:
                    continue
                for _id, ax in zip(ids, self.axs):
                    if amplitude.waveform_id.get_seed_string() == _id:
                        self.drawAmplitude(ax, amplitude, main_axes=True)
                        break
                else:
                    for ax in self.axs[1:]:
                        self.drawAmplitude(ax, amplitude, main_axes=False)
        for ax, xlims_, ylims_ in zip(self.axs, xlims, ylims):
            ax.set_xlim(xlims_)
            ax.set_ylim(ylims_)

    def drawPick(self, ax, pick, main_axes):
        if not pick.time:
            return

        if main_axes:
            alpha_line = 1
            alpha_span = 0.3
        else:
            alpha_line = 0.3
            alpha_span = 0.04

        color = self.seismic_phases[pick.phase_hint]
        reltime = self.time_abs2rel(pick.time)
        ax.axvline(reltime, color=color,
                   linewidth=AXVLINEWIDTH,
                   ymin=0, ymax=1, alpha=alpha_line)
        if pick.time_errors.lower_uncertainty or pick.time_errors.upper_uncertainty:
            if pick.time_errors.lower_uncertainty:
                time = reltime - pick.time_errors.lower_uncertainty
                ax.axvline(time, color=color,
                           linewidth=AXVLINEWIDTH,
                           ymin=0.25, ymax=0.75, alpha=alpha_line)
                ax.axvspan(time, reltime, color=color, alpha=alpha_span,
                           lw=None)
            if pick.time_errors.upper_uncertainty:
                time = reltime + pick.time_errors.upper_uncertainty
                ax.axvline(time, color=color,
                           linewidth=AXVLINEWIDTH,
                           ymin=0.25, ymax=0.75, alpha=alpha_line)
                ax.axvspan(reltime, time, color=color, alpha=alpha_span,
                           lw=None)
        elif pick.time_errors.uncertainty:
            time1 = reltime - pick.time_errors.uncertainty
            ax.axvline(time1, color=color,
                       linewidth=AXVLINEWIDTH,
                       ymin=0.25, ymax=0.75, alpha=alpha_line)
            time2 = reltime + pick.time_errors.uncertainty
            ax.axvline(time2, color=color,
                       linewidth=AXVLINEWIDTH,
                       ymin=0.25, ymax=0.75, alpha=alpha_line)
            ax.axvspan(time1, time2, color=color, alpha=alpha_span,
                       lw=None)

    def drawArrival(self, ax, arrival, pick, main_axes):
        if not pick.time or arrival.time_residual is None:
            return

        if main_axes:
            alpha_line = 1
        else:
            alpha_line = 0.3

        color = "k"
        time = self.time_abs2rel(pick.time)
        reltime = time - arrival.time_residual
        ax.axvline(reltime, color=color,
                   linewidth=AXVLINEWIDTH,
                   ymin=0, ymax=1, alpha=alpha_line)
        if main_axes:
            ax.axvspan(time, reltime, color=color, alpha=0.2)

    def drawAmplitude(self, ax, amplitude, scaling=None, main_axes=True):
        if self.widgets.qToolButton_physical_units.isChecked():
            self.error("Not displaying an amplitude pick set on raw count data.")
            return
        if main_axes:
            color = self._magnitude_color
        else:
            color = "gray"

        x, y = [], []
        if amplitude.low is not None:
            x.append(self.time_abs2rel(amplitude.low_time))
            y.append(amplitude.low)
        if amplitude.high is not None:
            x.append(self.time_abs2rel(amplitude.high_time))
            y.append(amplitude.high)
        if scaling is not None:
            y = [y_ * scaling for y_ in y]
        if x:
            ax.plot(x, y, linestyle="", markersize=MAG_MARKER['size'],
                    markeredgewidth=MAG_MARKER['edgewidth'],
                    color=color,
                    marker=MAG_MARKER['marker'], zorder=20)
        if len(x) == 2:
            ax.axvspan(x[0], x[1], color=color, alpha=0.2)
            ax.axhspan(y[0], y[1], color=color, alpha=0.1)

    def delPick(self, pick):
        event = self.catalog[0]
        if pick in event.picks:
            event.picks.remove(pick)

    def delAmplitude(self, amplitude):
        event = self.catalog[0]
        if amplitude in event.amplitudes:
            event.amplitudes.remove(amplitude)

    def getPick(self, network=None, station=None, phase_hint=None, waveform_id=None, axes=None, setdefault=False, seed_string=None):
        """
        returns first matching pick, does NOT ensure there is only one!
        if setdefault is True then if no pick is found an empty one is returned and inserted into self.picks.
        """
        picks = self.catalog[0].picks
        for p in picks:
            if network is not None and network != p.waveform_id.network_code:
                continue
            if station is not None and station != p.waveform_id.station_code:
                continue
            if phase_hint is not None and phase_hint != p.phase_hint:
                continue
            if waveform_id is not None and waveform_id != p.waveform_id:
                continue
            if seed_string is not None and seed_string != p.waveform_id.get_seed_string():
                continue
            if axes is not None:
                _i = self.axs.index(axes)
                _id = self.getCurrentStream()[_i].id
                phase_hint = self.getCurrentPhase()
                if p.waveform_id.get_seed_string() != _id:
                    continue
                if p.phase_hint != phase_hint:
                    continue
            return p
        if setdefault:
            # XXX TODO check if handling of picks/arrivals with regard to
            # resource ids is safe (overwritten picks, arrivals get deleted
            # etc., association of picks/arrivals is ok)
            # also check if setup of resource id strings make sense in general
            # (make versioning of methods possible, etc)
            if seed_string is None:
                raise Exception("Pick setdefault needs seed_string and phase_hint kwargs")
            p = Pick(seed_string=seed_string, phase_hint=phase_hint)
            picks.append(p)
            return p
        else:
            return None

    def getPicks(self, network, station, location=None):
        """
        returns all matching picks as list.
        """
        picks = self.catalog[0].picks
        ret = []
        for p in picks:
            if network != p.waveform_id.network_code:
                continue
            if station != p.waveform_id.station_code:
                continue
            if location is not None:
                if location != p.waveform_id.location_code:
                    continue
            ret.append(p)
        return ret

    def getAmplitude(self, network=None, station=None, waveform_id=None, axes=None, setdefault=False, seed_string=None):
        """
        returns first matching amplitude, does NOT ensure there is only one!
        if setdefault is True then if no arrival is found an empty one is returned and inserted into self.arrivals.
        """
        amplitudes = self.catalog[0].amplitudes
        for a in amplitudes:
            if network is not None and network != a.waveform_id.network_code:
                continue
            if station is not None and station != a.waveform_id.station_code:
                continue
            if waveform_id is not None and waveform_id != a.waveform_id:
                continue
            if seed_string is not None and seed_string != a.waveform_id.get_seed_string():
                continue
            if axes is not None:
                _i = self.axs.index(axes)
                _id = self.getCurrentStream()[_i].id
                if a.waveform_id.get_seed_string() != _id:
                    continue
            return a
        if setdefault:
            # XXX TODO check if handling of picks/arrivals with regard to
            # resource ids is safe (overwritten picks, arrivals get deleted
            # etc., association of picks/arrivals is ok)
            # also check if setup of resource id strings make sense in general
            # (make versioning of methods possible, etc)
            if seed_string is None:
                raise Exception("Arrival setdefault needs seed_string kwarg")
            self.debug(seed_string)
            a = Amplitude(seed_string=seed_string)
            amplitudes.append(a)
            return a
        else:
            return None

    def getAmplitudes(self, network, station, location):
        """
        returns all matching amplitudes as list.
        """
        amplitudes = self.catalog[0].amplitudes
        ret = []
        for a in amplitudes:
            if network != a.waveform_id.network_code:
                continue
            if station != a.waveform_id.station_code:
                continue
            if location != a.waveform_id.location_code:
                continue
            ret.append(a)
        return ret

    def getTrace(self, seed_string):
        """
        returns matching trace, does NOT ensure there is only one!
        """
        network, station, location, channel = seed_string.split(".")
        st = self.getStream(network, station, location)
        self.debug("seed_string: %s" % seed_string)
        self.debug(str(st))
        if st is None:
            return None
        st = st.select(channel=channel)
        self.debug(str(st))
        if not st:
            return None
        #if len(st) > 1:
        #    err = ("Warning: More than one trace matching:\n%s\n"
        #           "This should not happen. Using first Trace.") % str(st)
        #    self.error(err)
        return st[0]

    def getStream(self, network=None, station=None, location=None):
        """
        returns matching stream, does NOT ensure there is only one!
        """
        self.debug("net: %s, sta: %s,loc: %s" % (network, station, location))
        st = Stream()
        for st_ in self.streams:
            st += st_
        for st_ in self.streams_bkp:
            st += st_.copy()
        self.debug(str(st))
        st = st.select(network=network, station=station,
                       location=location)
        self.debug(str(st))
        st.merge(-1)
        self.debug(str(st))
        if st:
            return st
        return None

    def getStationMagnitude(self, network, station, location):
        """
        returns matching station magnitude, does NOT ensure there is only one!
        """
        try:
            stamags = self.catalog[0].station_magnitudes
        except:
            return None
        for stamag in stamags:
            wid = stamag.waveform_id
            if network != wid.network_code:
                continue
            if station != wid.station_code:
                continue
            if location != wid.location_code:
                continue
            return stamag
        return None

    def update_origin_azimuthal_gap(self):
        origin = self.catalog[0].origins[0]
        arrivals = origin.arrivals
        picks = self.catalog[0].picks
        azims = {}
        for a in arrivals:
            p = getPickForArrival(picks, a)
            if p is None:
                msg = ("Could not find pick for arrival. Aborting calculation "
                       "of azimuthal gap.")
                self.error(msg)
                return
            netsta = ".".join([p.waveform_id.network_code, p.waveform_id.station_code])
            azim = a.azimuth
            if azim is None:
                msg = ("Arrival's azimuth is 'None'. "
                       "Calculated azimuthal gap might be wrong")
                self.error(msg)
            else:
                azims.setdefault(netsta, []).append(azim)
        self.debug("Arrival azimuths: %s" % azims)
        azim_list = []
        for netsta in azims:
            tmp_list = azims.get(netsta, [])
            if not tmp_list:
                msg = ("No azimuth information for station %s. "
                       "Aborting calculation of azimuthal gap.")
                self.error(msg)
                return
            azim_list.append((np.median(tmp_list), netsta))
        azim_list = sorted(azim_list)
        azims = np.array([azim for azim, netsta in azim_list])
        azims.sort()
        # calculate azimuthal gap
        gaps = azims - np.roll(azims, 1)
        gaps[0] += 360.0
        gap = gaps.max()
        i_ = gaps.argmax()
        netstas = (azim_list[i_][1], azim_list[i_-1][1])
        if origin.quality is None:
            origin.quality = OriginQuality()
        origin.quality.azimuthal_gap = gap
        self.info("Azimuthal gap of %s between stations %s" % (gap, netstas))
        # calculate secondary azimuthal gap
        gaps = azims - np.roll(azims, 2)
        gaps[0] += 360.0
        gaps[1] += 360.0
        gap = gaps.max()
        i_ = gaps.argmax()
        netstas = (azim_list[i_][1], azim_list[i_-2][1])
        origin.quality.secondary_azimuthal_gap = gap
        self.info(("Secondary azimuthal gap of "
                   "%s between stations %s" % (gap, netstas)))

    def removeDuplicatePicks(self):
        """
        Makes sure that any waveform_id/phase_hint combination is unique in
        picks. Leave first occurence, remove all others and warn.

        XXX should be called when fetching an event.
        """
        picks = self.catalog[0].picks
        _ids = [p.waveform_id for p in picks]
        _phase_hints = [p.phase_hint for p in picks]
        msg = "For picks, any waveform_id / phase_hint combination must " + \
              "be unique. Some non-unique picks were removed:"
        for _id in _ids:
            for _phase_hint in _phase_hints:
                picks = [p for p in picks
                         if p.phase_hint == _phase_hint
                         and p.waveform_id == _id]
                if len(picks) > 1:
                    self.critical(msg)
                    for p in picks[1:]:
                        self.critical(str(p))
                        self.catalog[0].picks.remove(p)

    def setPick(self, pick):
        """
        Replace stored pick with given pick object.
        """
        picks = self.catalog[0].picks
        old = self.getPick(waveform_id=pick.waveform_id, phase_hint=pick.phase_hint)
        picks.remove(old)
        picks.append(pick)

    def getEventFromSeisHub(self, resource_name):
        """
        Fetch a Resource XML from SeisHub
        """
        client = self.event_server
        resource_xml = client.event.get_resource(resource_name)

        # parse quakeml
        catalog = readQuakeML(StringIO(resource_xml))
        self.setEventFromCatalog(catalog)
        ev = catalog[0]
        self.critical("Fetched event %i of %i: %s (public: %s, user: %s)"% \
              (self.seishubEventCurrent + 1, self.seishubEventCount,
               resource_name, self.widgets.qCheckBox_public.isChecked(),
               ev.creation_info.author))

    def setEventFromFilename(self, filename):
        """
        Set the currently active Event/Catalog from a filename of a QuakeML
        file.
        """
        # parse quakeml
        catalog = readQuakeML(filename)
        self.setEventFromCatalog(catalog)
        self.critical("Loaded event from file: %s" % filename)

    def setEventFromCatalog(self, catalog):
        """
        Set the currently active Event/Catalog.
        """
        self.catalog = catalog

        merge_events_in_catalog = self._get_config_value(
            'misc', 'merge_catalog', default=False)
        if merge_events_in_catalog:
            msg = ('Warning: Option to merge events in the catalog is highly '
                   'experimental and should only be used for reviewing '
                   'existing events.')
            self.error(msg)
            merge_events_in_catalog(self.catalog)

        bytes_io = BytesIO()
        catalog.write(bytes_io, format="QUAKEML")
        bytes_io.seek(0)
        self.update_qml_text(bytes_io.read().decode('UTF-8'))
        ev = self.catalog[0]

        public = ev.get("extra", {}).get('public', {}).get('value', "false")
        if public in ("True", "true"):
            public = True
        elif public in ("False", "false"):
            public = False
        self.widgets.qCheckBox_public.setChecked(public)

        # parse quakeML event type and select right one or add a custom one
        index = 0
        event_quakeml_type = ev.event_type
        if event_quakeml_type is not None:
            index = self.widgets.qComboBox_eventType.findText(event_quakeml_type.lower(), Qt.MatchExactly)
            if index == -1:
                self.widgets.qComboBox_eventType.addItem(event_quakeml_type)
                index = self.widgets.qComboBox_eventType.findText(event_quakeml_type.lower(), Qt.MatchExactly)
        self.widgets.qComboBox_eventType.setCurrentIndex(index)

        # remove duplicate picks (unless explicitly opted out by user, added by
        # request in #42):
        try:
            allow_multiple_picks = self.config.getboolean(
                "misc", "allow_multiple_picks_with_same_seed_id")
        except:
            allow_multiple_picks = False
        if not allow_multiple_picks:
            self.removeDuplicatePicks()

        # XXX TODO: do we need this!?
        # analyze amplitudes (magnitude picks):
        for ampl in ev.amplitudes:
            # only works for events created with obspyck as only in that case
            # we know the logic of the magnitude picking
            if "/obspyck/" not in str(ampl.method_id) or str(ampl.method_id).endswith("/obspyck/1"):
                msg = "Skipping amplitude not set with obspyck (or with old version)."
                self.error(msg)
                continue
            tr = self.getTrace(ampl.waveform_id.get_seed_string())
            if tr is None:
                continue
            ampl.setFromTimeWindow(tr)

        # XXX TODO: set station_magnitudes' "used" attribute depending if
        # they are involved in the magnitude calculation!!!
        try:
            magnitude = ev.magnitudes[0]
        except IndexError:
            magnitude = None
        for stamag in ev.station_magnitudes:
            stamag.used = False
            if magnitude is None:
                continue
            for contrib in magnitude.station_magnitude_contributions:
                if contrib.station_magnitude_id == stamag.resource_id:
                    if contrib.weight:
                        stamag.used = True

        if ev.focal_mechanisms:
            pref_fm = ev.preferred_focal_mechanism()
            if pref_fm:
                try:
                    self.focMechCurrent = ev.focal_mechanisms.index(pref_fm)
                except ValueError:
                    self.focMechCurrent = 0
            else:
                self.focMechCurrent = 0
        else:
            self.focMechCurrent = None

    def updateEventListFromSeisHub(self, starttime, endtime):
        """
        Searches for events in the database and stores a list of resource
        names. All events with at least one pick set in between start- and
        endtime are returned.

        :param starttime: Start datetime as UTCDateTime
        :param endtime: End datetime as UTCDateTime
        """
        self.checkForSysopEventDuplicates(self.T0, self.T1)

        events = self.event_server.event.get_list(min_last_pick=starttime,
                                                  max_first_pick=endtime)
        events.sort(key=lambda x: x['resource_name'])
        self.seishubEventList = events
        self.seishubEventCount = len(events)
        # we set the current event-pointer to the last list element, because we
        # iterate the counter immediately when fetching the first event...
        self.seishubEventCurrent = len(events) - 1
        msg = "%i events are available from SeisHub" % len(events)
        for event in events:
            resource_name = event.get('resource_name', "???")
            public = event.get('public', "???")
            author = event.get('author', "???")
            msg += "\n  - %s (public: %s, author: %s)" % (resource_name,
                                                          public, author)
        self.critical(msg)

    def checkForSysopEventDuplicates(self, starttime, endtime):
        """
        checks if there is more than one public event with picks in between
        starttime and endtime. if that is the case, a warning is issued.
        the user should then resolve this conflict by deleting events until
        only one instance remains.
        at the moment this check is conducted for the current timewindow when
        submitting a sysop event.
        """
        events = self.event_server.event.get_list(min_last_pick=starttime,
                                                  max_first_pick=endtime)
        # XXX TODO: we don't have sysop as author anymore!
        # all controlled by public tag now.
        sysop_events = []
        for ev in events:
            public = ev.get("public", False)
            if public:
                sysop_events.append(str(ev.get("resource_name", "???")))

        # if there is a possible duplicate, pop up a warning window and print a
        # warning in the GUI error textview:
        if len(sysop_events) > 1:
            err = "ObsPyck found more than one public event with picks in " + \
                  "the current time window! Please check if these are " + \
                  "duplicate events and delete old resources."
            errlist = "\n".join(sysop_events)
            self.error(err)
            self.error(errlist)
            qMessageBox = QtWidgets.QMessageBox()
            app_icon = QtGui.QIcon()
            app_icon.addFile(ICON_PATH.format("_16x16"), QtCore.QSize(16, 16))
            app_icon.addFile(ICON_PATH.format("_24x42"), QtCore.QSize(24, 24))
            app_icon.addFile(ICON_PATH.format("_32x32"), QtCore.QSize(32, 32))
            app_icon.addFile(ICON_PATH.format("_48x48"), QtCore.QSize(48, 48))
            app_icon.addFile(ICON_PATH.format(""), QtCore.QSize(64, 64))
            qMessageBox.setWindowIcon(app_icon)
            qMessageBox.setIcon(QtWidgets.QMessageBox.Critical)
            qMessageBox.setWindowTitle("Possible Duplicate Public Event!")
            qMessageBox.setText(err)
            qMessageBox.setInformativeText(errlist)
            qMessageBox.setStandardButtons(QtWidgets.QMessageBox.Ok)
            qMessageBox.exec_()

    def checkForCompleteEvent(self):
        """
        checks if the event has the necessary information a sysop event should
        have::

          - datetime (origin time)
          - longitude/latitude/depth
          - magnitude
          - used_p/used_s
        """
        # XXX TODO not checking for used-P and used-S-count
        # XXX causes problems for parsing in website?
        event = self.catalog[0]
        try:
            assert(len(event.origins) > 0), "event has no origin"
            o = event.origins[0]
            assert(o.latitude is not None), "origin has no latitude"
            assert(o.longitude is not None), "origin has no longitude"
            assert(o.depth is not None), "origin has no depth"
            assert(o.time is not None), "origin has no origin time"
            assert(len(event.magnitudes) > 0), "event has no magnitude"
            m = self.catalog[0].magnitudes[0]
            assert(m.mag is not None), "magnitude has no magnitude value"
        except Exception as e:
            return False, str(e)
        return True, None

    def popupBadEventError(self, msg):
        """
        pop up an error window indicating that event information is missing
        """
        # XXX TODO could show more detailed message, e.g. whats's missing
        # or resource name or link to resource in seihub.
        err = ("The public event to submit misses some mandatory information:"
               " %s." % msg)
        self.error(err)
        qMessageBox = QtWidgets.QMessageBox()
        app_icon = QtGui.QIcon()
        app_icon.addFile(ICON_PATH.format("_16x16"), QtCore.QSize(16, 16))
        app_icon.addFile(ICON_PATH.format("_24x42"), QtCore.QSize(24, 24))
        app_icon.addFile(ICON_PATH.format("_32x32"), QtCore.QSize(32, 32))
        app_icon.addFile(ICON_PATH.format("_48x48"), QtCore.QSize(48, 48))
        app_icon.addFile(ICON_PATH.format(""), QtCore.QSize(64, 64))
        qMessageBox.setWindowIcon(app_icon)
        qMessageBox.setIcon(QtWidgets.QMessageBox.Critical)
        qMessageBox.setWindowTitle("Public Event with Missing Information!")
        qMessageBox.setText(err)
        qMessageBox.setStandardButtons(QtWidgets.QMessageBox.Abort)
        qMessageBox.exec_()


def main():
    """
    Gets executed when the program starts.
    """
    usage = (
        "\n %prog -t 2010-08-01T12:00:00 -d 30 "
        "[local waveform or station metadata files]"
        "\n\nGet all available options with: %prog -h")
    parser = optparse.OptionParser(usage)
    for opt_args, opt_kwargs in COMMANDLINE_OPTIONS:
        parser.add_option(*opt_args, **opt_kwargs)
    (options, args) = parser.parse_args()
    # read config file
    if options.config_file:
        config_file = os.path.expanduser(options.config_file)
    else:
        config_file = os.path.join(os.path.expanduser("~"), ".obspyckrc")
        if not os.path.exists(config_file):
            src = os.path.join(
                os.path.dirname(__file__), "example.cfg")
            shutil.copy(src, config_file)
            print("created example config file: {}".format(config_file))

    if options.time is None:
        msg = 'Time option ("-t", "--time") must be specified.'
        raise Exception(msg)
    print("Running ObsPyck version {} (location: {})".format(__version__,
                                                             __file__))
    print("using config file: {}".format(config_file))
    config = SafeConfigParser(allow_no_value=True)
    # make all config keys case sensitive
    config.optionxform = str
    config.read(config_file)
    # set matplotlibrc changes specified in config (if any)
    set_matplotlib_defaults(config)

    # TODO: remove KEYS variable and lookup from config directly
    KEYS = {key: config.get('keys', key) for key in config.options('keys')}
    check_keybinding_conflicts(KEYS)
    (clients, streams, inventories) = fetch_waveforms_with_metadata(options, args, config)
    # Create the GUI application
    qApp = QtWidgets.QApplication(sys.argv)
    obspyck = ObsPyck(clients, streams, options, KEYS, config, inventories)
    # qApp.connect(qApp, QtCore.SIGNAL("aboutToQuit()"), obspyck.cleanup)
    qApp.aboutToQuit.connect(obspyck.cleanup)
    os._exit(qApp.exec_())


if __name__ == "__main__":
    main()
