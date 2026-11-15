@file:Suppress("SetTextI18n", "UseKtx", "SpellCheckingInspection", "DEPRECATION", "PrivatePropertyName")

package com.example.gyro

import android.animation.ObjectAnimator
import android.animation.PropertyValuesHolder
import android.animation.ValueAnimator
import android.app.Activity
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.Locale
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.min

class MainActivity : Activity(), SensorEventListener {

    // ---------------------------------------------------------------- palette
    private val cBgTop = Color.rgb(11, 12, 16)
    private val cBgBottom = Color.rgb(22, 24, 32)
    private val cCard = Color.rgb(27, 30, 39)
    private val cField = Color.rgb(17, 19, 25)
    private val cStroke = Color.rgb(44, 48, 62)
    private val cText = Color.rgb(240, 242, 247)
    private val cDim = Color.rgb(135, 144, 162)
    private val cAccent = Color.rgb(79, 140, 255)
    private val cRed = Color.rgb(255, 72, 92)
    private val cRedDark = Color.rgb(44, 22, 28)
    private val cGreen = Color.rgb(52, 211, 153)
    private val cAmber = Color.rgb(251, 191, 36)
    private val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
    private val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

    // ---------------------------------------------------------------- sensor / network
    private lateinit var sensorManager: SensorManager
    private var sensor: Sensor? = null
    private var sensorThread: HandlerThread? = null
    private var sensorHandler: Handler? = null
    @Volatile private var socket: DatagramSocket? = null
    @Volatile private var target: InetSocketAddress? = null
    @Volatile private var streaming = false
    @Volatile private var recording = false
    @Volatile private var recenterCount = 0
    @Volatile private var sentCount = 0
    @Volatile private var lastAck = 0L              // last reply from Blender (uptime ms)
    @Volatile private var blenderTarget = ""
    private var streamStart = 0L
    private var seq = 0L
    private val q = FloatArray(4)
    private val rot = FloatArray(9)

    // orientation for the on-screen indicator (written by the sensor thread)
    @Volatile private var upX = 0f
    @Volatile private var upY = 1f
    @Volatile private var upZ = 0f
    @Volatile private var heading = 0f
    private var headingZero = 0f

    // ---------------------------------------------------------------- views
    private lateinit var root: LinearLayout
    private lateinit var ipField: EditText
    private lateinit var portField: EditText
    private lateinit var connectButton: TextView
    private lateinit var linkInfo: TextView
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var horizon: HorizonView
    private lateinit var rollValue: TextView
    private lateinit var tiltValue: TextView
    private lateinit var headingValue: TextView
    private lateinit var recordTile: LinearLayout
    private lateinit var recordTitle: TextView
    private var pulse: ObjectAnimator? = null
    private val ui = Handler(Looper.getMainLooper())
    private var lastRateTime = 0L
    private var rate = 0
    private var error: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN)
        window.statusBarColor = cBgTop
        window.navigationBarColor = cBgBottom
        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
        sensor = sensorManager.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
            ?: sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
        buildUi()
        if (sensor == null) {
            error = "No rotation sensor"
            connectButton.isEnabled = false
        }
        updateStatus()
    }

    // ---------------------------------------------------------------- UI helpers

    private fun dp(v: Float) = v * resources.displayMetrics.density
    private fun dpi(v: Float) = dp(v).toInt()

    private fun rounded(color: Int, radius: Float, stroke: Int? = null) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radius)
        if (stroke != null) setStroke(dpi(1f), stroke)
    }

    private fun pressable(d: GradientDrawable) =
        RippleDrawable(ColorStateList.valueOf(Color.argb(50, 255, 255, 255)), d, null)

    private fun label(s: String, size: Float, color: Int, style: String = "sans-serif") =
        TextView(this).apply {
            text = s
            textSize = size
            setTextColor(color)
            typeface = Typeface.create(style, Typeface.NORMAL)
        }

    private fun caption(s: String) = label(s, 11f, cDim, "sans-serif-medium").apply {
        letterSpacing = 0.14f
    }

    private fun lp(w: Int, h: Int, weight: Float = 0f, top: Float = 0f, start: Float = 0f) =
        LinearLayout.LayoutParams(w, h, weight).apply {
            topMargin = dpi(top)
            marginStart = dpi(start)
        }

    private fun card() = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(cCard, 26f, cStroke)
        setPadding(dpi(18f), dpi(16f), dpi(18f), dpi(18f))
        elevation = dp(6f)
    }

    private fun field(value: String, hint: String, type: Int) = EditText(this).apply {
        setText(value)
        this.hint = hint
        inputType = type
        isSingleLine = true
        imeOptions = EditorInfo.IME_ACTION_DONE
        textSize = 17f
        setTextColor(cText)
        setHintTextColor(cDim)
        typeface = Typeface.MONOSPACE
        background = rounded(cField, 14f, cStroke)
        setPadding(dpi(14f), dpi(12f), dpi(14f), dpi(12f))
    }

    private fun tile(title: String, hint: String, bg: Int, stroke: Int): Pair<LinearLayout, TextView> {
        val t = label(title, 19f, cText, "sans-serif-medium")
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            background = pressable(rounded(bg, 28f, stroke))
            isClickable = true
            isFocusable = true
            elevation = dp(6f)
            addView(t)
            addView(caption(hint).apply { setPadding(0, dpi(4f), 0, 0) })
        }
        return box to t
    }

    private fun stat(name: String, parent: LinearLayout): TextView {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
        }
        val v = label("--", 20f, cText, "monospace")
        col.addView(v)
        col.addView(caption(name).apply { setPadding(0, dpi(2f), 0, 0) })
        parent.addView(col, lp(0, WRAP, 1f))
        return v
    }

    private fun buzz(v: View = window.decorView) {
        v.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
    }

    // ---------------------------------------------------------------- UI

    private fun buildUi() {
        val prefs = getSharedPreferences("phone_gyro", MODE_PRIVATE)

        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM, intArrayOf(cBgTop, cBgBottom)
            )
        }
        // keep content clear of the status / navigation bars (edge-to-edge on Android 15+)
        root.setOnApplyWindowInsetsListener { v, insets ->
            val top: Int
            val bottom: Int
            if (Build.VERSION.SDK_INT >= 30) {
                val i = insets.getInsets(WindowInsets.Type.systemBars())
                top = i.top
                bottom = i.bottom
            } else {
                top = insets.systemWindowInsetTop
                bottom = insets.systemWindowInsetBottom
            }
            v.setPadding(dpi(20f), top + dpi(14f), dpi(20f), bottom + dpi(18f))
            insets
        }

        // header
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val titles = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        titles.addView(label("Phone Gyro", 28f, cText, "sans-serif-medium"))
        titles.addView(label("Motion controller for Blender", 13f, cDim))
        header.addView(titles, lp(0, WRAP, 1f))

        val pill = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = rounded(cField, 50f, cStroke)
            setPadding(dpi(12f), dpi(7f), dpi(14f), dpi(7f))
        }
        statusDot = View(this).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(cDim)
            }
        }
        pill.addView(statusDot, lp(dpi(8f), dpi(8f)))
        statusText = label("Offline", 12f, cText, "sans-serif-medium")
        pill.addView(statusText, lp(WRAP, WRAP, start = 8f))
        header.addView(pill)
        root.addView(header)

        // connection card
        val conn = card()
        conn.addView(caption("BLENDER PC"))
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        ipField = field(
            prefs.getString("ip", "192.168.1.10")!!, "IP address",
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        )
        portField = field(prefs.getInt("port", 9876).toString(), "Port", InputType.TYPE_CLASS_NUMBER)
        row.addView(ipField, lp(0, WRAP, 2.2f))
        row.addView(portField, lp(0, WRAP, 1f, start = 10f))
        conn.addView(row, lp(MATCH, WRAP, top = 10f))
        connectButton = label("Connect", 16f, Color.WHITE, "sans-serif-medium").apply {
            gravity = Gravity.CENTER
            background = pressable(rounded(cAccent, 50f))
            setPadding(0, dpi(14f), 0, dpi(14f))
            setOnClickListener {
                buzz(it)
                if (streaming) stopStreaming() else startStreaming()
            }
        }
        conn.addView(connectButton, lp(MATCH, WRAP, top = 14f))
        linkInfo = label("Start Listening in Blender, then Connect", 12f, cDim).apply {
            gravity = Gravity.CENTER
        }
        conn.addView(linkInfo, lp(MATCH, WRAP, top = 10f))
        root.addView(conn, lp(MATCH, WRAP, top = 18f))

        // attitude card
        val att = card()
        att.addView(caption("ATTITUDE"))
        horizon = HorizonView(this)
        att.addView(horizon, lp(MATCH, 0, 1f, top = 8f))
        val stats = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        tiltValue = stat("TILT", stats)
        rollValue = stat("ROLL", stats)
        headingValue = stat("HEADING", stats)
        att.addView(stats, lp(MATCH, WRAP, top = 10f))
        root.addView(att, lp(MATCH, 0, 1f, top = 14f))

        // controls
        val controls = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val (recenter, _) = tile("⟲  Recenter", "VOL −", cCard, cStroke)
        recenter.setOnClickListener {
            buzz(it)
            doRecenter()
        }
        val (rec, recTitle) = tile("●  Record", "VOL +", cRedDark, Color.rgb(92, 40, 52))
        recordTile = rec
        recordTitle = recTitle
        recordTitle.setTextColor(cRed)
        rec.setOnClickListener {
            buzz(it)
            if (streaming) setRecording(!recording)
        }
        controls.addView(recenter, lp(0, MATCH, 1f))
        controls.addView(rec, lp(0, MATCH, 1f, start = 12f))
        root.addView(controls, lp(MATCH, dpi(108f), top = 14f))

        setContentView(root)
        updateRecordAvailability()
    }

    private fun doRecenter() {
        recenterCount++
        headingZero = heading
    }

    // Record only makes sense while connected: otherwise Blender would start recording on connect
    private fun updateRecordAvailability() {
        recordTile.alpha = if (streaming) 1f else 0.4f
        recordTile.isEnabled = streaming
    }

    private fun setRecording(on: Boolean) {
        recording = on
        recordTile.background = pressable(
            if (on) rounded(cRed, 28f, cRed) else rounded(cRedDark, 28f, Color.rgb(92, 40, 52))
        )
        recordTitle.text = if (on) "■  Stop" else "●  Record"
        recordTitle.setTextColor(if (on) Color.WHITE else cRed)
        pulse?.cancel()
        recordTile.scaleX = 1f
        recordTile.scaleY = 1f
        if (on) {
            pulse = ObjectAnimator.ofPropertyValuesHolder(
                recordTile,
                PropertyValuesHolder.ofFloat(View.SCALE_X, 1f, 1.04f),
                PropertyValuesHolder.ofFloat(View.SCALE_Y, 1f, 1.04f)
            ).apply {
                duration = 650
                repeatCount = ValueAnimator.INFINITE
                repeatMode = ValueAnimator.REVERSE
                start()
            }
        }
    }

    private fun setDot(color: Int) {
        (statusDot.background as GradientDrawable).setColor(color)
    }

    private fun updateStatus() {
        val e = error
        val now = SystemClock.uptimeMillis()
        val acked = streaming && now - lastAck < 1500
        when {
            e != null -> {
                setDot(cRed)
                statusText.text = e
            }
            acked -> {
                setDot(cGreen)
                statusText.text = "Live · $rate Hz"
            }
            streaming -> {
                setDot(cAmber)
                statusText.text = if (rate > 0) "Sending…" else "Starting…"
            }
            else -> {
                setDot(cDim)
                statusText.text = "Offline"
            }
        }
        linkInfo.text = when {
            !streaming -> "Start Listening in Blender, then Connect"
            acked -> "Connected to Blender · driving ${blenderTarget.ifEmpty { "target" }}"
            now - streamStart > 3000 -> "No reply from Blender: check the address, Wi-Fi and firewall"
            else -> "Looking for Blender…"
        }
        linkInfo.setTextColor(
            when {
                acked -> cGreen
                streaming && now - streamStart > 3000 -> cAmber
                else -> cDim
            }
        )
    }

    // volume keys work as remote buttons while connected
    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        val vol = keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP
        if (streaming && vol) {
            if (event.repeatCount == 0) {
                buzz()
                if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) doRecenter() else setRecording(!recording)
            }
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    // ---------------------------------------------------------------- 30 fps UI loop

    private fun fmt(v: Float) = String.format(Locale.US, "%+.1f°", v)

    private val frame = object : Runnable {
        override fun run() {
            horizon.invalidate()
            val tilt = Math.toDegrees(asin((-upZ).coerceIn(-1f, 1f)).toDouble()).toFloat()
            val roll = -Math.toDegrees(atan2(upX, upY).toDouble()).toFloat()
            var h = heading - headingZero
            while (h > 180f) h -= 360f
            while (h < -180f) h += 360f
            tiltValue.text = fmt(tilt)
            rollValue.text = fmt(roll)
            headingValue.text = fmt(h)
            val now = SystemClock.uptimeMillis()
            if (now - lastRateTime >= 1000) {
                rate = sentCount
                sentCount = 0
                lastRateTime = now
            }
            if (now % 250 < 40) updateStatus()      // ~4 times a second is plenty
            ui.postDelayed(this, 33)
        }
    }

    // ---------------------------------------------------------------- lifecycle

    override fun onResume() {
        super.onResume()
        val t = HandlerThread("gyro").also { it.start() }
        sensorThread = t
        sensorHandler = Handler(t.looper)
        // 10 ms = 100 Hz; the callback runs on the background thread
        sensor?.let { sensorManager.registerListener(this, it, 10_000, sensorHandler) }
        ui.post(frame)
    }

    override fun onPause() {
        super.onPause()
        stopStreaming()
        sensorManager.unregisterListener(this)
        sensorThread?.quitSafely()
        sensorThread = null
        sensorHandler = null
        ui.removeCallbacks(frame)
    }

    // ---------------------------------------------------------------- streaming

    private fun startStreaming() {
        val ip = ipField.text.toString().trim()
        val port = portField.text.toString().toIntOrNull()
        if (ip.isEmpty() || port == null || port !in 1..65535) {
            error = "Check address / port"
            updateStatus()
            return
        }
        error = null
        getSharedPreferences("phone_gyro", MODE_PRIVATE).edit()
            .putString("ip", ip).putInt("port", port).apply()
        (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager)
            .hideSoftInputFromWindow(root.windowToken, 0)
        ipField.clearFocus()
        portField.clearFocus()

        lastAck = 0L
        blenderTarget = ""
        streamStart = SystemClock.uptimeMillis()
        sensorHandler?.post {
            try {
                val s = DatagramSocket()
                socket = s
                target = InetSocketAddress(ip, port)
                seq = 0
                startAckListener(s)
            } catch (_: Exception) {
                ui.post {
                    error = "Network error"
                    stopStreaming()
                }
            }
        }
        streaming = true
        ipField.isEnabled = false
        portField.isEnabled = false
        connectButton.text = "Disconnect"
        connectButton.background = pressable(rounded(cField, 50f, cStroke))
        updateRecordAvailability()
        updateStatus()
    }

    // Blender answers "GYACK <target>" about twice a second
    private fun startAckListener(s: DatagramSocket) {
        Thread {
            val buf = ByteArray(128)
            try {
                s.soTimeout = 500
            } catch (_: Exception) {
            }
            while (streaming && !s.isClosed) {
                try {
                    val pkt = DatagramPacket(buf, buf.size)
                    s.receive(pkt)
                    val txt = String(pkt.data, 0, pkt.length, Charsets.US_ASCII)
                    if (txt.startsWith("GYACK")) {
                        lastAck = SystemClock.uptimeMillis()
                        blenderTarget = txt.removePrefix("GYACK").trim()
                    }
                } catch (_: SocketTimeoutException) {
                    // nothing this time
                } catch (_: Exception) {
                    break                            // socket closed
                }
            }
        }.apply {
            isDaemon = true
            start()
        }
    }

    private fun stopStreaming() {
        streaming = false
        val s = socket
        socket = null
        target = null
        sensorHandler?.post { s?.close() } ?: s?.close()
        if (recording) setRecording(false)
        rate = 0
        lastAck = 0L
        if (::connectButton.isInitialized) {
            ipField.isEnabled = true
            portField.isEnabled = true
            connectButton.text = "Connect"
            connectButton.background = pressable(rounded(cAccent, 50f))
            updateRecordAvailability()
            updateStatus()
        }
    }

    override fun onSensorChanged(event: SensorEvent) {
        val v = event.values
        SensorManager.getRotationMatrixFromVector(rot, v)
        upX = rot[6]                                   // world "up" expressed in phone axes
        upY = rot[7]
        upZ = rot[8]
        heading = Math.toDegrees(atan2(-rot[2].toDouble(), -rot[5].toDouble())).toFloat()

        if (!streaming) return
        val s = socket ?: return
        val a = target ?: return
        SensorManager.getQuaternionFromVector(q, v)    // w, x, y, z
        val msg = "GY1 $seq ${q[0]} ${q[1]} ${q[2]} ${q[3]} ${if (recording) 1 else 0} $recenterCount"
        seq++
        try {
            val data = msg.toByteArray(Charsets.US_ASCII)
            s.send(DatagramPacket(data, data.size, a))
            sentCount++
        } catch (_: Exception) {
            // Wi-Fi hiccup: skip this sample
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    // ---------------------------------------------------------------- artificial horizon

    inner class HorizonView(ctx: Context) : View(ctx) {
        private val sky = Paint(Paint.ANTI_ALIAS_FLAG)
        private val ground = Paint(Paint.ANTI_ALIAS_FLAG)
        private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE
            strokeWidth = dp(2f)
        }
        private val ladder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.argb(120, 255, 255, 255)
            strokeWidth = dp(1.2f)
        }
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = dp(2f)
            color = cStroke
        }
        private val reticle = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = cAccent
            strokeWidth = dp(3.5f)
            strokeCap = Paint.Cap.ROUND
        }
        private val clip = Path()
        private var radius = 0f

        override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
            radius = min(w, h) / 2f - dp(4f)
            clip.reset()
            clip.addCircle(w / 2f, h / 2f, radius, Path.Direction.CW)
            sky.shader = LinearGradient(
                0f, -radius * 2, 0f, 0f,
                Color.rgb(14, 36, 82), Color.rgb(52, 118, 214), Shader.TileMode.CLAMP
            )
            ground.shader = LinearGradient(
                0f, 0f, 0f, radius * 2,
                Color.rgb(84, 62, 40), Color.rgb(28, 21, 14), Shader.TileMode.CLAMP
            )
        }

        override fun onDraw(canvas: Canvas) {
            if (radius <= 0f) return
            val cx = width / 2f
            val cy = height / 2f
            val r = radius
            val pxPerDeg = r / 45f
            val tilt = Math.toDegrees(asin((-upZ).coerceIn(-1f, 1f)).toDouble()).toFloat()
            val roll = Math.toDegrees(atan2(upX, upY).toDouble()).toFloat()

            canvas.save()
            canvas.clipPath(clip)
            canvas.translate(cx, cy)
            canvas.rotate(roll)
            canvas.translate(0f, tilt * pxPerDeg)
            canvas.drawRect(-r * 3, -r * 3, r * 3, 0f, sky)
            canvas.drawRect(-r * 3, 0f, r * 3, r * 3, ground)
            canvas.drawLine(-r * 3, 0f, r * 3, 0f, line)
            for (i in -8..8) {                          // pitch ladder every 10°
                if (i == 0) continue
                val y = -i * 10 * pxPerDeg
                val w = if (i % 2 == 0) r * 0.34f else r * 0.18f
                canvas.drawLine(-w, y, w, y, ladder)
            }
            canvas.restore()

            canvas.drawCircle(cx, cy, r, ring)
            canvas.drawLine(cx - r * 0.5f, cy, cx - r * 0.16f, cy, reticle)
            canvas.drawLine(cx + r * 0.16f, cy, cx + r * 0.5f, cy, reticle)
            canvas.drawCircle(cx, cy, dp(4f), reticle)
        }
    }
}
