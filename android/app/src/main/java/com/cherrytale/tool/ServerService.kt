package com.cherrytale.tool

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread

/**
 * 前台服务：承载内嵌 Python HTTP 服务器（127.0.0.1:8000）。
 *
 * 【为什么必须用前台服务（2026-10-02 真机排障定案）】
 * 服务器原本跑在 Activity 的守护线程里。App 一旦切后台 / 锁屏，系统会把它
 * 当"缓存进程"**整体 SIGSTOP 冻结**（vivo 的清理器更激进）：进程活着、
 * 端口还在内核层握手，但所有线程永不调度 —— 表现就是
 * "点击执行没反应、日志不输出、连健康检查都挂死"（模拟器上 kill -STOP
 * 已 1:1 复现并验证）。前台服务带常驻通知，系统不会冻结/清理它，
 * 手机锁屏后任务照常跑。
 */
class ServerService : Service() {

    override fun onCreate() {
        super.onCreate()
        startForegroundNotification()
        startPythonServer()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int =
        START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startForegroundNotification() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "工具台本地服务",
                NotificationManager.IMPORTANCE_LOW
            )
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("米娅小助手")
            .setContentText("本地服务运行中 · 127.0.0.1:8000")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)
    }

    /**
     * 把 APK assets 里的游戏配置表解压到应用目录。
     *
     * 任务运行时要读 `Cherrytale Asset/TextAsset/` 下的配置表；sync 脚本把
     * 代码引用的表打进了 assets/material/，assets 只能经 AssetManager 读流，
     * Python 侧需要真实文件路径 —— 所以复制成普通文件。全量几 MB，
     * 每次启动直接覆盖解压，保证升级 APK 后表也是新的。
     */
    private fun extractMaterialAssets() {
        val targetRoot = File(filesDir, "material")
        fun copyDir(assetPath: String, dest: File) {
            val children = assets.list(assetPath) ?: return
            dest.mkdirs()
            for (child in children) {
                val childPath = "$assetPath/$child"
                val childDest = File(dest, child)
                if ((assets.list(childPath) ?: arrayOf()).isNotEmpty()) {
                    copyDir(childPath, childDest)
                } else {
                    assets.open(childPath).use { input ->
                        childDest.outputStream().use { output -> input.copyTo(output) }
                    }
                }
            }
        }
        copyDir("material", targetRoot)
    }

    private fun startPythonServer() {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        val dataDir = filesDir.absolutePath
        extractMaterialAssets()
        thread(name = "python-server", start = true, isDaemon = true) {
            try {
                val py = Python.getInstance()
                val environ = py.getModule("os").get("environ")
                // 可写数据目录（会话 / 账号库 / 缓存与日志），见 config.py 的路径体系
                environ?.callAttr("__setitem__", "CHERRYTALE_DATA_ROOT", dataDir)
                // 配置表目录：上面从 assets 解压出来的位置
                // （config.py 的 TEXT_ASSET_DIR = MATERIAL_ROOT / "Cherrytale Asset" / "TextAsset"）
                environ?.callAttr(
                    "__setitem__",
                    "CHERRYTALE_MATERIAL_ROOT",
                    File(filesDir, "material").absolutePath
                )
                // 【安卓上必须关掉系统信任库】truststore 在安卓没有对应证书目录，
                // 退回 certifi（随 requests 自动装入）。
                environ?.callAttr("__setitem__", "CHERRYTALE_USE_SYSTEM_TRUST_STORE", "0")

                // serve_forever 阻塞本守护线程 —— 前台服务常驻，正合适。
                val serverModule = py.getModule("webapi.app")
                serverModule.callAttr("main")
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }
    }

    companion object {
        private const val CHANNEL_ID = "cherrytale_server"
        private const val NOTIFICATION_ID = 1
    }
}
