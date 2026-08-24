package com.zaydcluster.mailpush

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessaging

class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val tv = TextView(this)
        tv.setPadding(64, 64, 64, 64)
        tv.setTextIsSelectable(true)
        setContentView(tv)
        requestNotificationPermission()
        try {
            FirebaseMessaging.getInstance().token
                .addOnCompleteListener { task ->
                    tv.text = if (task.isSuccessful) {
                        "FCM Token (salin ke server):\n\n${task.result}"
                    } else {
                        "Gagal mengambil token: ${task.exception?.message}"
                    }
                }
        } catch (e: Exception) {
            tv.text = "Error inisialisasi:\n\n${e.javaClass.simpleName}: ${e.message}"
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(
                    this, Manifest.permission.POST_NOTIFICATIONS
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1
                )
            }
        }
    }
}
