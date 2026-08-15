package online.scratchforge.trade;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.util.Base64;
import android.view.KeyEvent;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;

/**
 * Native shell around the trade.scratchforge mobile web app.
 *
 * Deliberately dependency-free — no AndroidX, no Trusted Web Activity — so the
 * APK builds with nothing but the platform SDK and installs on any Android 6+
 * phone. The WebView is Chromium, the same engine the site is developed
 * against, so the chart, canvases and websocket behave identically.
 */
public class MainActivity extends Activity {

    private static final String HOST  = "trade.scratchforge.online";
    private static final String START = "https://" + HOST + "/m.html";
    private static final int    BG    = 0xFF07090D;

    private WebView web;

    /**
     * The web app exports CSV and TXT by building a blob: URL and clicking a
     * download link. A WebView can't hand a blob to the system DownloadManager,
     * so this shim catches the click, reads the blob back as base64 and passes
     * it to {@link Bridge#saveBase64} to be written to the phone's storage.
     */
    private static final String DOWNLOAD_SHIM =
        "(function(){if(window.__sfDL)return;window.__sfDL=1;" +
        "document.addEventListener('click',function(e){" +
        "  var n=e.target;" +
        "  while(n&&n!==document){ if(n.tagName==='A'&&n.hasAttribute('download'))break; n=n.parentNode; }" +
        "  if(!n||n===document||n.tagName!=='A')return;" +
        "  var h=n.getAttribute('href')||'';" +
        "  if(h.indexOf('blob:')!==0&&h.indexOf('data:')!==0)return;" +
        "  e.preventDefault();e.stopPropagation();" +
        "  fetch(h).then(function(r){return r.blob();}).then(function(b){" +
        "    var fr=new FileReader();" +
        "    fr.onloadend=function(){var s=String(fr.result);var i=s.indexOf(',');" +
        "      SFAndroid.saveBase64(n.getAttribute('download')||'download', s.slice(i+1));};" +
        "    fr.readAsDataURL(b);" +
        "  }).catch(function(err){SFAndroid.toast('Export failed: '+err);});" +
        "},true);})();";

    public class Bridge {
        @JavascriptInterface
        public void saveBase64(String name, String b64) {
            final String safe = (name == null || name.trim().isEmpty())
                    ? "download" : name.replaceAll("[\\\\/:*?\"<>|]", "_");
            String message;
            try {
                byte[] data = Base64.decode(b64, Base64.DEFAULT);
                // App-specific external dir: writable on every API level with
                // no storage permission, and visible over USB / in Files.
                File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                if (dir == null) dir = getFilesDir();
                if (!dir.exists() && !dir.mkdirs()) throw new Exception("cannot create " + dir);
                File out = new File(dir, safe);
                FileOutputStream fos = new FileOutputStream(out);
                try { fos.write(data); } finally { fos.close(); }
                message = "Saved " + safe + " to " + out.getParent();
            } catch (Throwable t) {
                message = "Could not save " + safe + ": " + t.getMessage();
            }
            toast(message);
        }

        @JavascriptInterface
        public void toast(final String msg) {
            runOnUiThread(new Runnable() {
                public void run() {
                    Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show();
                }
            });
        }
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        getWindow().setBackgroundDrawableResource(R.color.backgroundColor);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            getWindow().setStatusBarColor(BG);
            getWindow().setNavigationBarColor(BG);
        }

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#07090d"));
        setContentView(web);

        // Android 15+ forces edge-to-edge for targetSdk 35+, so the status bar
        // and the gesture pill draw straight over the page — the bottom tab
        // bar's middle label ends up under the pill. A WebView doesn't feed
        // those insets to CSS env(safe-area-inset-*), so inset the view itself.
        // The window background is the same colour, so the bars blend in.
        web.setOnApplyWindowInsetsListener(new android.view.View.OnApplyWindowInsetsListener() {
            @Override
            public android.view.WindowInsets onApplyWindowInsets(
                    android.view.View v, android.view.WindowInsets insets) {
                int l, t, r, b;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    android.graphics.Insets i = insets.getInsets(
                            android.view.WindowInsets.Type.systemBars()
                                    | android.view.WindowInsets.Type.displayCutout());
                    l = i.left; t = i.top; r = i.right; b = i.bottom;
                } else {
                    l = insets.getSystemWindowInsetLeft();
                    t = insets.getSystemWindowInsetTop();
                    r = insets.getSystemWindowInsetRight();
                    b = insets.getSystemWindowInsetBottom();
                }
                v.setPadding(l, t, r, b);
                return insets;
            }
        });
        // Insets are dispatched when the view attaches, which already happened
        // in setContentView above — ask for another pass now the listener exists.
        web.requestApplyInsets();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadsImagesAutomatically(true);
        s.setUseWideViewPort(true);          // honour <meta name="viewport">
        s.setLoadWithOverviewMode(false);
        s.setSupportZoom(false);             // the chart does its own pinch-zoom
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        }

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);            // the session cookie lives here
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cm.setAcceptThirdPartyCookies(web, true);
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        web.addJavascriptInterface(new Bridge(), "SFAndroid");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return route(url);
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                v.evaluateJavascript(DOWNLOAD_SHIM, null);
            }
        });

        // A shortcut or a tapped link may hand us a specific URL.
        Uri data = getIntent() != null ? getIntent().getData() : null;
        String start = START;
        if (data != null && HOST.equals(data.getHost())) start = data.toString();
        else if (getIntent() != null) {
            String extra = getIntent().getStringExtra("url");
            if (extra != null && extra.startsWith("https://" + HOST)) start = extra;
        }

        if (saved != null) web.restoreState(saved);
        else web.loadUrl(start);
    }

    /** Keep our own host in the app; hand anything else to the real browser. */
    private boolean route(String url) {
        if (url == null) return false;
        Uri u = Uri.parse(url);
        if (HOST.equals(u.getHost())) return false;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (Exception ignored) { }
        return true;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent == null) return;
        Uri d = intent.getData();
        if (d != null && HOST.equals(d.getHost())) { web.loadUrl(d.toString()); return; }
        String extra = intent.getStringExtra("url");
        if (extra != null && extra.startsWith("https://" + HOST)) web.loadUrl(extra);
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent ev) {
        if (code == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(code, ev);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (web != null) web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) web.onPause();
        CookieManager.getInstance().flush();   // don't lose the session on kill
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }
}
