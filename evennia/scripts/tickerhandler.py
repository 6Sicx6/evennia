"""
TickerHandler

This implements an efficient Ticker which uses a subscription
model to 'tick' subscribed objects at regular intervals.

The ticker mechanism is used by importing and accessing
the instantiated TICKER_HANDLER instance in this module. This
instance is run by the server; it will save its status across
server reloads and be started automaticall on boot.

Example:

```python
    from evennia.scripts.tickerhandler import TICKER_HANDLER

    # tick myobj every 15 seconds
    TICKER_HANDLER.add(myobj, 15)
```

The handler will by default try to call a hook `at_tick()`
on the subscribing object. The hook's name can be changed
if the `hook_key` keyword is given to the `add()` method (only
one such alternate name per interval though). The
handler will transparently set up and add new timers behind
the scenes to tick at given intervals, using a TickerPool.

To remove:

```python
    TICKER_HANDLER.remove(myobj, 15)
```

The interval must be given since a single object can be subscribed
to many different tickers at the same time.


The TickerHandler's functionality can be overloaded by modifying the
Ticker class and then changing TickerPool and TickerHandler to use the
custom classes

```python
class MyTicker(Ticker):
    # [doing custom stuff]

class MyTickerPool(TickerPool):
    ticker_class = MyTicker
class MyTickerHandler(TickerHandler):
    ticker_pool_class = MyTickerPool
```

If one wants to duplicate TICKER_HANDLER's auto-saving feature in
a  custom handler one can make a custom `AT_STARTSTOP_MODULE` entry to
call the handler's `save()` and `restore()` methods when the server reboots.

"""
import inspect
from builtins import object

from twisted.internet.defer import inlineCallbacks
from django.core.exceptions import ObjectDoesNotExist
from evennia.scripts.scripts import ExtendedLoopingCall
from evennia.server.models import ServerConfig
from evennia.utils.logger import log_trace, log_err
from evennia.utils.dbserialize import dbserialize, dbunserialize
from evennia.utils import variable_from_module

_GA = object.__getattribute__
_SA = object.__setattr__


_ERROR_ADD_TICKER = \
"""TickerHandler: Tried to add an invalid ticker:
{storekey}
Ticker was not added."""

class Ticker(object):
    """
    Represents a repeatedly running task that calls
    hooks repeatedly. Overload `_callback` to change the
    way it operates.
    """

    @inlineCallbacks
    def _callback(self):
        """
        This will be called repeatedly every `self.interval` seconds.
        `self.subscriptions` contain tuples of (obj, args, kwargs) for
        each subscribing object.

        If overloading, this callback is expected to handle all
        subscriptions when it is triggered. It should not return
        anything and should not traceback on poorly designed hooks.
        The callback should ideally work under @inlineCallbacks so it
        can yield appropriately.

        The _hook_key, which is passed down through the handler via
        kwargs is used here to identify which hook method to call.

        """
        to_remove = []
        for store_key, (args, kwargs) in self.subscriptions.iteritems():
            callback = yield kwargs.pop("_callback", "at_tick")
            obj = yield kwargs.pop("_obj", None)
            try:
                if callable(callback):
                    # call directly
                    yield callback(*args, **kwargs)
                    continue
                # try object method
                if not obj or not obj.pk:
                    # object was deleted between calls
                    to_remove.append(store_key)
                    continue
                else:
                    yield _GA(obj, callback)(*args, **kwargs)
            except ObjectDoesNotExist:
                log_trace("Removing ticker.")
                to_remove.append(store_key)
            except Exception:
                log_trace()
            finally:
                # make sure to re-store
                kwargs["_callback"] = callback
                kwargs["_obj"] = obj
        # cleanup
        for store_key in to_remove:
            self.remove(store_key)

    def __init__(self, interval):
        """
        Set up the ticker

        Args:
            interval (int): The stepping interval.

        """
        self.interval = interval
        self.subscriptions = {}
        # set up a twisted asynchronous repeat call
        self.task = ExtendedLoopingCall(self._callback)

    def validate(self, start_delay=None):
        """
        Start/stop the task depending on how many subscribers we have
        using it.

        Args:
            start_delay (int): Time to way before starting.

        """
        subs = self.subscriptions
        if self.task.running:
            if not subs:
                self.task.stop()
        elif subs:
            self.task.start(self.interval, now=False, start_delay=start_delay)

    def add(self, store_key, *args, **kwargs):
        """
        Sign up a subscriber to this ticker.
        Args:
            store_key (str): Unique storage hash for this ticker subscription.
            args (any, optional): Arguments to call the hook method with.

        Kwargs:
            _start_delay (int): If set, this will be
                used to delay the start of the trigger instead of
                `interval`.

        """
        start_delay = kwargs.pop("_start_delay", None)
        self.subscriptions[store_key] = (args, kwargs)
        self.validate(start_delay=start_delay)

    def remove(self, store_key):
        """
        Unsubscribe object from this ticker

        Args:
            store_key (str): Unique store key.

        """
        self.subscriptions.pop(store_key, False)
        self.validate()

    def stop(self):
        """
        Kill the Task, regardless of subscriptions.

        """
        self.subscriptions = {}
        self.validate()


class TickerPool(object):
    """
    This maintains a pool of
    `evennia.scripts.scripts.ExtendedLoopingCall` tasks for calling
    subscribed objects at given times.

    """
    ticker_class = Ticker

    def __init__(self):
        """
        Initialize the pool.

        """
        self.tickers = {}

    def add(self, store_key, *args, **kwargs):
        """
        Add new ticker subscriber.

        Args:
            store_key (str): Unique storage hash.
            args (any, optional): Arguments to send to the hook method.

        """
        _, _, _, interval, _, _ = store_key
        if not interval:
            log_err(_ERROR_ADD_TICKER.format(store_key=store_key))
            return

        if interval not in self.tickers:
            self.tickers[interval] = self.ticker_class(interval)
        self.tickers[interval].add(store_key, *args, **kwargs)

    def remove(self, store_key):
        """
        Remove subscription from pool.

        Args:
            store_key (str): Unique storage hash to remove

        """
        _, _, _, interval, _, _ = store_key
        if interval in self.tickers:
            self.tickers[interval].remove(store_key)
            if not self.tickers[interval]:
                del self.tickers[interval]

    def stop(self, interval=None):
        """
        Stop all scripts in pool. This is done at server reload since
        restoring the pool will automatically re-populate the pool.

        Args:
            interval (int, optional): Only stop tickers with this
                interval.

        """
        if interval and interval in self.tickers:
            self.tickers[interval].stop()
        else:
            for ticker in self.tickers.values():
                ticker.stop()


class TickerHandler(object):
    """
    The Tickerhandler maintains a pool of tasks for subscribing
    objects to various tick rates.  The pool maintains creation
    instructions and and re-applies them at a server restart.

    """
    ticker_pool_class = TickerPool

    def __init__(self, save_name="ticker_storage"):
        """
        Initialize handler

        save_name (str, optional): The name of the ServerConfig
            instance to store the handler state persistently.

        """
        self.ticker_storage = {}
        self.save_name = save_name
        self.ticker_pool = self.ticker_pool_class()

    def _get_callback(self, callback):
        """
        Analyze callback and determine its consituents

        Args:
            callback (function or method): This is either a stand-alone
                function or class method on a typeclassed entitye (that is,
                an entity that can be saved to the database).

        Returns:
            ret (tuple): This is a tuple of the form `(obj, path, callfunc)`,
                where `obj` is the database object the callback is defined on
                if it's a method (otherwise `None`) and vice-versa, `path` is
                the python-path to the stand-alone function (`None` if a method).
                The `callfunc` is either the name of the method to call or the
                callable function object itself.

        """
        outobj, outpath, outcallfunc = None, None, None
        if callable(callback):
            if inspect.ismethod(callback):
                outobj = callback.im_self
                outcallfunc = callback.im_func.func_name
            elif inspect.isfunction(callback):
                outpath = "%s.%s" % (callback.__module__, callback.func_name)
                outcallfunc = callback
        else:
            raise TypeError("%s is not a callable function or method." %  callback)
        return outobj, outpath, outcallfunc

    def _store_key(self, obj, path, interval, callfunc, idstring="", persistent=True):
        """
        Tries to create a store_key for the object.

        Args:
            obj (Object or None): Subscribing object if any.
            path (str or None): Python-path to callable, if any.
            interval (int): Ticker interval.
            callfunc (callable or str): This is either the callable function or
                the name of the method to call. Note that the callable is never
                stored in the key; that is uniquely identified with the python-path.
            idstring (str, optional): Additional separator between
                different subscription types.
            persistent (bool, optional): If this ticker should survive a system
                shutdown or not.

        Returns:
            isdb_and_store_key (tuple): A tuple `(obj, path, interval,
                methodname, idstring)` that uniquely identifies the
                ticker. `path` is `None` and `methodname` is the name of
                the method if `obj_or_path` is a database object.
                Vice-versa, `obj` and `methodname` are `None` if
                `obj_or_path` is a python-path.

        """
        interval = int(interval)
        persistent = bool(persistent)
        outobj = obj if obj and hasattr(obj, "db_key") else None
        outpath = path if path and isinstance(path, basestring) else None
        methodname = callfunc if callfunc and isinstance(callfunc, basestring) else None
        return (outobj, methodname, outpath, interval, idstring, persistent)

    def save(self):
        """
        Save ticker_storage as a serialized string into a temporary
        ServerConf field. Whereas saving is done on the fly, if called
        by server when it shuts down, the current timer of each ticker
        will be saved so it can start over from that point.

        """
        if self.ticker_storage:
            start_delays = dict((interval, ticker.task.next_call_time())
                                 for interval, ticker in self.ticker_pool.tickers.items())
            # update the timers for the tickers
            #for (obj, interval, idstring), (args, kwargs) in self.ticker_storage.items():
            for store_key, (args, kwargs) in self.ticker_storage.items():
                interval = store_key[1]
                # this is a mutable, so it's updated in-place in ticker_storage
                kwargs["_start_delay"] = start_delays.get(interval, None)
            ServerConfig.objects.conf(key=self.save_name,
                                    value=dbserialize(self.ticker_storage))
        else:
            # make sure we have nothing lingering in the database
            ServerConfig.objects.conf(key=self.save_name, delete=True)

    def restore(self, server_reload=True):
        """
        Restore ticker_storage from database and re-initialize the
        handler from storage. This is triggered by the server at
        restart.

        Args:
            server_reload (bool, optional): If this is False, it means
                the server went through a cold reboot and all
                non-persistent tickers must be killed.

        """
        # load stored command instructions and use them to re-initialize handler
        restored_tickers = ServerConfig.objects.conf(key=self.save_name)
        if restored_tickers:
            # the dbunserialize will convert all serialized dbobjs to real objects

            restored_tickers = dbunserialize(restored_tickers)
            ticker_storage = {}
            for store_key, (args, kwargs) in restored_tickers.iteritems():
                try:
                    obj, methodname, path, interval, idstring, persistent = store_key
                    if not persistent and not server_reload:
                        # this ticker will not be restarted
                        continue
                    if obj and methodname:
                        kwargs["_callback"] = methodname
                        kwargs["_obj"] = obj
                    elif path:
                        modname, varname = path.rsplit(".", 1)
                        callback = variable_from_module(modname, varname)
                        kwargs["_callback"] = callback
                        kwargs["_obj"] = None
                    ticker_storage[store_key] = (args, kwargs)
                except Exception as err:
                    # this suggests a malformed save or missing objects
                    log_err("%s\nTickerhandler: Removing malformed ticker: %s" % (err, str(store_key)))
                    continue
                self.ticker_storage = ticker_storage
                self.ticker_pool.add(store_key, *args, **kwargs)

    def add(self, interval=60, callback=None, idstring="", persistent=True, *args, **kwargs):
        """
        Add subscription to tickerhandler

        Args:
            interval (int, optional): Interval in seconds between calling
                `callable(*args, **kwargs)`
            callable (callable function or method, optional): This
                should either be a stand-alone function or a method on a
                typeclassed entity (that is, one that can be saved to the
                database).
            idstring (str, optional): Identifier for separating
                this ticker-subscription from others with the same
                interval. Allows for managing multiple calls with
                the same time interval and callback.
            persistent (bool, optional): A ticker will always survive
                a server reload. If this is unset, the ticker will be
                deleted by a server shutdown.
            args, kwargs (optional): These will be passed into the
                callback every time it is called.

        Notes:
            The callback will be identified by type and stored either as
            as combination of serialized database object + methodname or
            as a python-path to the module + funcname. These strings will
            be combined iwth `interval` and `idstring` to define a
            unique storage key for saving. These must thus all be supplied
            when wanting to modify/remove the ticker later.

        """
        obj, path, callfunc = self._get_callback(callback)
        store_key = self._store_key(obj, path, interval, callfunc, idstring, persistent)
        self.ticker_storage[store_key] = (args, kwargs)
        self.save()
        kwargs["_obj"] = obj
        kwargs["_callback"] = callfunc # either method-name or callable
        self.ticker_pool.add(store_key, *args, **kwargs)

    def remove(self, interval=60, callback=None, idstring=""):
        """
        Remove object from ticker or only remove it from tickers with
        a given interval.

        Args:
            interval (int, optional): Interval of ticker to remove.
            callback (callable function or method): Either a function or
                the method of a typeclassed object.
            idstring (str, optional): Identifier id of ticker to remove.

        """
        obj, path, callfunc = self._get_callback(callback)
        store_key = self._store_key(obj, path, interval, callfunc, idstring)
        to_remove = self.ticker_storage.pop(store_key, None)
        if to_remove:
            self.ticker_pool.remove(store_key)
            self.save()

    def clear(self, interval=None):
        """
        Stop/remove tickers from handler.

        Args:
            interval (int): Only stop tickers with this interval.

        Notes:
            This is the only supported way to kill tickers related to
            non-db objects.

        """
        self.ticker_pool.stop(interval)
        if interval:
            self.ticker_storage = dict((store_key, store_key)
                                        for store_key in self.ticker_storage
                                        if store_key[1] != interval)
        else:
            self.ticker_storage = {}
        self.save()

    def all(self, interval=None):
        """
        Get all subscriptions.

        Args:
            interval (int): Limit match to tickers with this interval.

        Returns:
            tickers (list): If `interval` was given, this is a list of
                tickers using that interval.
            tickerpool_layout (dict): If `interval` was *not* given,
                this is a dict {interval1: [ticker1, ticker2, ...],  ...}

        """
        if interval is None:
            # return dict of all, ordered by interval
            return dict((interval, ticker.subscriptions)
                         for interval, ticker in self.ticker_pool.tickers.iteritems())
        else:
            # get individual interval
            ticker = self.ticker_pool.tickers.get(interval, None)
            if ticker:
                return {interval: ticker.subscriptions}

    def all_display(self):
        """
        Get all tickers on an easily displayable form.

        Returns:
            tickers (dict): A list of all storekeys

        """
        store_keys = []
        for ticker in self.ticker_pool.tickers.itervalues():
            store_keys.extend([store_key for store_key in ticker.subscriptions])
        return store_keys

# main tickerhandler
TICKER_HANDLER = TickerHandler()
