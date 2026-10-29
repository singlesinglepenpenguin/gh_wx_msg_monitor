"""截取本工具自身窗口，不截取微信，也不触发初始化。"""
import ctypes
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from group_msg_monitor.app import App

folder = ROOT/'data/diagnostics'
folder.mkdir(exist_ok=True)

class Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]
class Size(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]
class Rect(ctypes.Structure):
    _fields_ = [('origin', Point), ('size', Size)]

objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
objc.objc_getClass.restype, objc.objc_getClass.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
objc.sel_registerName.restype, objc.sel_registerName.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
def call(obj, selector, restype=ctypes.c_void_p, argtypes=(), args=()):
    fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(('objc_msgSend', objc))
    return fn(obj, objc.sel_registerName(selector.encode()), *args)
def cls(name):
    return objc.objc_getClass(name.encode())
def capture(title, path):
    windows = call(call(cls('NSApplication'), 'sharedApplication'), 'windows')
    for i in range(call(windows, 'count', ctypes.c_ulong)):
        win = call(windows, 'objectAtIndex:', argtypes=(ctypes.c_ulong,), args=(i,))
        name = call(call(win, 'title'), 'UTF8String', ctypes.c_char_p)
        if name and name.decode() == title:
            view = call(win, 'contentView')
            bounds = call(view, 'bounds', Rect)
            rep = call(view, 'bitmapImageRepForCachingDisplayInRect:', argtypes=(Rect,), args=(bounds,))
            call(view, 'cacheDisplayInRect:toBitmapImageRep:', None, (Rect, ctypes.c_void_p), (bounds, rep))
            data = call(rep, 'representationUsingType:properties:', argtypes=(ctypes.c_ulong, ctypes.c_void_p), args=(4, call(cls('NSDictionary'), 'dictionary')))
            name = call(cls('NSString'), 'stringWithUTF8String:', argtypes=(ctypes.c_char_p,), args=(str(path).encode(),))
            assert call(data, 'writeToFile:atomically:', ctypes.c_bool, (ctypes.c_void_p, ctypes.c_bool), (name, True))
            return
    raise RuntimeError('Own application window not found: ' + title)

app = App()
errors = []
def visible_texts(widget):
    texts = []
    for child in widget.winfo_children():
        if child.winfo_viewable() and 'text' in child.keys():
            texts.append(child.cget('text'))
        texts += visible_texts(child)
    return texts

def finish():
    try:
        capture('微信连接设置', folder/'wechat_settings.png')
        visible = visible_texts(app.settings_window)
        assert '普通初始化' not in visible and '新版微信初始化' not in visible
        assert all(label not in visible for label in ('重新检查微信', '排错说明', '运行日志', '故障记录'))
        def expand(widget):
            for child in widget.winfo_children():
                if 'text' in child.keys() and str(child.cget('text')).startswith('高级维护（'):
                    child.invoke()
                    return True
                if expand(child):
                    return True
            return False
        assert expand(app.settings_window)
        app.update_idletasks()
        visible = visible_texts(app.settings_window)
        assert all(label in visible for label in ('普通初始化', '新版微信初始化', '修复进程读取权限（会修改微信签名）'))
        capture('微信连接设置', folder/'maintenance.png')
        print('UI verified: saved credentials usable; initialization controls hidden.', flush=True)
    except Exception as exc:
        errors.append(exc)
    finally:
        app.destroy()

def start():
    try:
        assert app.connection_ok and not app.busy, app.vars['connection'].get()
        capture('群接龙更新工具', folder/'main_window.png')
        app._connection()
        app.after(250, finish)
    except Exception as exc:
        errors.append(exc)
        app.destroy()

app.after(1800, start)
app.mainloop()
if errors:
    raise errors[0]
print('Screenshots:', folder, flush=True)
