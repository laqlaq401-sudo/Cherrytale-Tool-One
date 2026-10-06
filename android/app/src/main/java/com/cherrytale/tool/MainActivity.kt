package com.cherrytale.tool

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ContentValues
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val serverPort = 8000

    /** 本机主页地址（加载与重试共用，避免字符串在多处各写一遍而对不上）。 */
    private val homeUrl: String
        get() = "http://127.0.0.1:$serverPort/"

    /** 本机主页已连续失败次数（加载成功后清零，见 webViewClient）。 */
    private var loadRetryCount = 0

    /** 上一次加载是否失败过：防止"错误页也会回调 onPageFinished"把计数清零。 */
    private var lastLoadFailed = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            loadWithOverviewMode = true
            useWideViewPort = true
            // 【禁止双指缩放（2026-10-02）】页面按手机宽度自适应排版后，
            // 双指捏合只会破坏布局，所以 WebView 与网页 viewport 两层都禁掉：
            // 这里关掉 WebView 的缩放引擎，页面 meta 里另有 user-scalable=no 兜底。
            setSupportZoom(false)
            setBuiltInZoomControls(false)
            setDisplayZoomControls(false)
            // 【锁定系统字体放大】安卓 WebView 默认按系统"字体大小"设置对页面
            // 文本做整体放大（textZoom 跟随系统），系统字体调大后 11~12px 的
            // 注释文字会被吹到正文大小，整版字号逻辑全部失真 —— 钉死 100%，
            // 字号完全交给页面 CSS 控制。
            setTextZoom(100)
        }

        // 【JS 桥（2026-10-06）】开发者日志导出到公共「下载」目录。
        // 为什么需要：见 LogExporter 的类注释 —— 安卓端 Python 的落盘位置是应用
        // **内部**私有存储，用户在「文件管理」里根本找不到。
        webView.addJavascriptInterface(LogExporter(), "MiyaAndroid")

        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                // 只有"确实加载成功"才把重试计数清零：服务没起来时 WebView
                // 仍可能回调本方法（错误页），无条件清零会让重试变成无限循环。
                if (!lastLoadFailed) loadRetryCount = 0
                lastLoadFailed = false
            }

            override fun onReceivedError(
                view: WebView?,
                errorCode: Int,
                description: String?,
                failingUrl: String?
            ) {
                // 只对本机主页重试（旧版回调本就只报主框架，子资源失败不会进来）。
                if (failingUrl != homeUrl) return
                lastLoadFailed = true
                // 【重试上限（2026-10-04 安全审计 P1）】
                // 后端服务拉起约需 1~2 秒，未就绪时延时重试是对的；
                // 但端口被占 / 服务永久起不来时，旧写法会**每秒无限重载**。
                // 现在最多重试 MAX_LOAD_RETRY 次，且 Activity 已销毁时不再碰 WebView。
                if (loadRetryCount >= MAX_LOAD_RETRY) return
                if (isFinishing || isDestroyed) return
                loadRetryCount++
                view?.postDelayed({
                    if (!isFinishing && !isDestroyed) webView.loadUrl(homeUrl)
                }, RETRY_DELAY_MS)
            }
        }

        // 【必须设置 WebChromeClient】没有它时 Android WebView 会静默丢弃
        // 页面的 alert/confirm —— 真实扫荡前的二次确认被吞掉并当作"取消"，
        // 表现就是"点了执行没反应"（2026-10-02 定案）。前端已换成自绘对话框，
        // 这里再兜底一层，保证任何遗留的原生弹窗也能显示。
        webView.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onJsAlert(
                view: WebView?,
                url: String?,
                message: String?,
                result: android.webkit.JsResult?
            ): Boolean {
                android.app.AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("知道了") { _, _ -> result?.confirm() }
                    .setOnCancelListener { result?.cancel() }
                    .show()
                return true
            }

            override fun onJsConfirm(
                view: WebView?,
                url: String?,
                message: String?,
                result: android.webkit.JsResult?
            ): Boolean {
                android.app.AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("确定") { _, _ -> result?.confirm() }
                    .setNegativeButton("取消") { _, _ -> result?.cancel() }
                    .setOnCancelListener { result?.cancel() }
                    .show()
                return true
            }
        }

        // 服务器搬进前台服务（见 ServerService 的说明）：App 切后台 / 锁屏后
        // 系统不再冻结进程，任务照常执行 —— 这是"点击没反应"问题的修复本体。
        startForegroundService(Intent(this, ServerService::class.java))

        // 延迟加载本地 WebUI
        webView.postDelayed({
            if (!isFinishing && !isDestroyed) webView.loadUrl(homeUrl)
        }, 1500)
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    /**
     * 注入到 WebView 的 JS 桥（`window.MiyaAndroid`）—— 把开发者日志导出到公共「下载」目录。
     *
     * 【为什么必须有它（2026-10-06 用户报障）】
     * 安卓端 `CHERRYTALE_DATA_ROOT` = `filesDir`（见 ServerService），于是 Python 把日志写进了
     * **应用内部私有存储** `/data/user/0/com.cherrytale.tool/files/logs/`。
     * 写入本身是成功的，但无 root 的「文件管理」「我的文件」以及 USB/MTP 一律读不到 ——
     * 用户点完「保存」在手机里怎么也找不到（连 Android/data 都比它可见，本路径还要更深一层）。
     * 而 targetSdk 34 的分区存储又禁止 Python 直接 `open()` 公共目录（必 EACCES），
     * 所以只能由 Kotlin 侧走 MediaStore 把文件**再复制一份**到
     * `下载/米娅小助手/` —— 那是任何文件管理器都能看到的位置。
     *
     * 【安全】只有从本机 `http://127.0.0.1:8000` 加载的页面能调到这里；两个方法都不接受任意写入
     * 目标，且 `exportLog` 对传入路径做 canonical 前缀校验，只允许读应用自己的私有目录。
     *
     * 【版本】`MediaStore.Downloads` 自 API 29 起可用；minSdk 26~28 上直接返回"不支持"，
     * 不申请任何存储权限（2026-10-06 决定：低版本 Android 8/9 不做导出，只保留应用内路径提示）。
     */
    inner class LogExporter {

        /**
         * 把应用私有目录里的日志文件复制到公共「下载/米娅小助手/」。
         *
         * @param srcPath  服务端 `/api/logs/save` 返回的绝对路径（必须位于应用私有目录内）
         * @param fileName 展示用文件名（会被清洗，防路径穿越）
         * @return JSON 字符串：`{"ok":true,"path":"下载/米娅小助手/xxx.txt"}`
         *         或 `{"ok":false,"message":"…"}`
         */
        @JavascriptInterface
        fun exportLog(srcPath: String, fileName: String): String {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
                return errorJson(
                    "当前系统（Android ${Build.VERSION.RELEASE}）不支持一键导出，日志已保存在应用内"
                )
            }

            val source = try {
                File(srcPath).canonicalFile
            } catch (e: Exception) {
                return errorJson("源文件路径非法，已拒绝导出")
            }
            val privateRoot = filesDir.canonicalPath + File.separator
            if (!source.path.startsWith(privateRoot)) {
                return errorJson("源文件不在应用数据目录内，已拒绝导出")
            }
            if (!source.isFile) {
                return errorJson("源文件不存在，导出失败")
            }

            val displayName = sanitizeFileName(fileName)
            val resolver = contentResolver
            val pending = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, displayName)
                put(MediaStore.Downloads.MIME_TYPE, "text/plain")
                put(
                    MediaStore.Downloads.RELATIVE_PATH,
                    "${Environment.DIRECTORY_DOWNLOADS}/$EXPORT_SUBDIR"
                )
                // 先置 IS_PENDING=1，字节写完后清零 —— 否则文件管理器里会看到半成品
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val target = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, pending)
                ?: return errorJson("系统未提供可写入位置（存储空间不足或被策略禁止）")

            try {
                val output = resolver.openOutputStream(target)
                    ?: return errorJson("无法打开输出流，导出失败")
                output.use { out -> source.inputStream().use { input -> input.copyTo(out) } }
                resolver.update(
                    target,
                    ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) },
                    null,
                    null
                )
            } catch (e: Exception) {
                runCatching { resolver.delete(target, null, null) }
                return errorJson("写入下载目录失败：${e.message ?: e.javaClass.simpleName}")
            }

            return JSONObject()
                .put("ok", true)
                .put("path", "下载/$EXPORT_SUBDIR/$displayName")
                .put("fileName", displayName)
                .toString()
        }

        /** 打开系统「下载」界面（Android 10+ 的 DocumentsUI 入口）。 */
        @JavascriptInterface
        fun openDownloadsFolder(): String {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
                return errorJson("当前系统不支持直接打开下载目录，请在文件管理器中查看「下载」")
            }
            val intent = Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
            // 本方法跑在 WebView 的 JavaBridge 线程上，切回 UI 线程再启动界面
            runOnUiThread {
                try {
                    startActivity(intent)
                } catch (e: Exception) {
                    // 个别精简 ROM 没有下载界面：吞掉即可，用户仍可在文件管理器里查看
                }
            }
            return JSONObject().put("ok", true).toString()
        }

        /** 清洗文件名：去掉文件系统非法字符防路径穿越；空名给个兜底。 */
        private fun sanitizeFileName(raw: String): String {
            val cleaned = raw.replace(Regex("""[\\/:*?"<>|]"""), "_").trim()
            return if (cleaned.isBlank()) "cherrytale-dev-log.txt" else cleaned
        }

        private fun errorJson(message: String): String =
            JSONObject().put("ok", false).put("message", message).toString()
    }

    companion object {
        /** 本机主页加载失败后最多重试几次（超过即停，不再无限重载）。 */
        private const val MAX_LOAD_RETRY = 5

        /** 每次重试的间隔（毫秒）。 */
        private const val RETRY_DELAY_MS = 1000L

        /** 日志导出目录名 —— 公共「下载」目录下的一级子目录。 */
        private const val EXPORT_SUBDIR = "米娅小助手"
    }
}
