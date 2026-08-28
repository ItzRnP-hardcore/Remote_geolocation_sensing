package com.example.imulogger

import org.junit.Test
import org.osmdroid.mapsforge.MapsForgeTileSource

class MapsforgeTest {
    @Test
    fun printConstructors() {
        val constructors = MapsForgeTileSource::class.java.constructors
        for (c in constructors) {
            println("CONSTRUCTOR: $c")
        }
        val methods = MapsForgeTileSource::class.java.methods
        for (m in methods) {
            val name = m.name.lowercase()
            if (name.contains("scale") || name.contains("text") || name.contains("size")) {
                println("METHOD: $m")
            }
        }
    }
}
