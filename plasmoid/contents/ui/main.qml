pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.components as PC3
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    readonly property string sourceCmd: "/usr/local/libexec/audiomux/audiomux-source.py"
    property int refreshMs: 1000
    property var diagnostics: ({})
    property var pendingSinkOffsets: ({})
    property var pendingSourceOffsets: ({})
    readonly property int pendingOffsetTimeoutMs: 3000

    property var sinks: []
    property var sources: []
    property string masterSink: ""
    property string syncStatus: ""
    property bool syncChecking: false

    preferredRepresentation: compactRepresentation
    Plasmoid.icon: "mixer-three-slider-symbolic"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus

    toolTipMainText: "AudioMux"
    toolTipSubText: "Audio multiplexer"
    toolTipTextFormat: Text.PlainText

    // ── helpers ───────────────────────────────────────────────────────────

    function pollNow() {
        executableSource.disconnectSource(root.sourceCmd)
        executableSource.connectSource(root.sourceCmd)
    }

    function runCmd(cmd) {
        cmdSource.disconnectSource(cmd)
        cmdSource.connectSource(cmd)
    }

    function reconcileOffsets(devices, pendingOffsets) {
        const now = Date.now()
        const nextPending = Object.assign({}, pendingOffsets)
        const nextDevices = devices.map(device => {
            const pending = nextPending[device.name]
            if (!pending)
                return device
            if (device.offset === pending.value || now - pending.updatedAt > root.pendingOffsetTimeoutMs) {
                delete nextPending[device.name]
                return device
            }
            return Object.assign({}, device, { offset: pending.value })
        })
        return { devices: nextDevices, pendingOffsets: nextPending }
    }

    function setPendingOffset(devices, pendingOffsets, name, offset) {
        const nextPending = Object.assign({}, pendingOffsets)
        nextPending[name] = {
            value: offset,
            updatedAt: Date.now()
        }
        const nextDevices = devices.map(device => device.name === name
            ? Object.assign({}, device, { offset: offset })
            : device)
        return { devices: nextDevices, pendingOffsets: nextPending }
    }

    function parseState(json) {
        try {
            const s = JSON.parse(json)
            const sinkState = reconcileOffsets(s.sinks || [], root.pendingSinkOffsets)
            const sourceState = reconcileOffsets(s.sources || [], root.pendingSourceOffsets)
            root.sinks = sinkState.devices
            root.sources = sourceState.devices
            root.pendingSinkOffsets = sinkState.pendingOffsets
            root.pendingSourceOffsets = sourceState.pendingOffsets
            root.diagnostics = s.diagnostics || {}
            root.masterSink = s.master_sink || ""
        } catch(e) {}
    }

    function onSinkToggled(name, active) {
        if (active)
            runCmd(root.sourceCmd + " set-sink-offset " + name + " 0")
        const actives = root.sinks
            .filter(s => s.name === name ? active : s.active)
            .map(s => s.name)
        runCmd(root.sourceCmd + " set-active-sinks " + actives.join(","))
    }

    function onSourceToggled(name, active) {
        if (active)
            runCmd(root.sourceCmd + " set-source-offset " + name + " 0")
        const actives = root.sources
            .filter(s => s.name === name ? active : s.active)
            .map(s => s.name)
        runCmd(root.sourceCmd + " set-active-sources " + actives.join(","))
    }

    function onSinkOffset(name, offset) {
        const sinkState = setPendingOffset(root.sinks, root.pendingSinkOffsets, name, offset)
        root.sinks = sinkState.devices
        root.pendingSinkOffsets = sinkState.pendingOffsets
        runCmd(root.sourceCmd + " set-sink-offset " + name + " " + offset)
    }

    function onSourceOffset(name, offset) {
        const sourceState = setPendingOffset(root.sources, root.pendingSourceOffsets, name, offset)
        root.sources = sourceState.devices
        root.pendingSourceOffsets = sourceState.pendingOffsets
        runCmd(root.sourceCmd + " set-source-offset " + name + " " + offset)
    }

    // ── compact: icon ─────────────────────────────────────────────────────

    compactRepresentation: Text {
        text: "\u{1F39B}\u{FE0F}"
        font.pixelSize: Kirigami.Units.iconSizes.smallMedium
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter

        TapHandler {
            onTapped: root.expanded = !root.expanded
        }
    }

    // ── full popup ─────────────────────────────────────────────────────────

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth:  Kirigami.Units.gridUnit * 28
        Layout.preferredWidth: Kirigami.Units.gridUnit * 28
        Layout.minimumHeight: Kirigami.Units.gridUnit * 18
        Layout.preferredHeight: Kirigami.Units.gridUnit * 22
        collapseMarginsHint: true

        // ── header bar ────────────────────────────────────────────────────
        header: PlasmaExtras.PlasmoidHeading {
            ColumnLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Text {
                        text: "\u{1F39B}\u{FE0F}"
                        font.pixelSize: Kirigami.Units.iconSizes.smallMedium
                    }

                    PlasmaExtras.Heading {
                        level: 1
                        text: "AudioMux"
                        Layout.fillWidth: true
                        leftPadding: Kirigami.Units.smallSpacing
                    }

                    PC3.Button {
                        text: root.syncChecking ? "Syncing…" : "Sync Delay"
                        icon.name: "chronometer"
                        enabled: !root.syncChecking && root.sinks.filter(s => s.active).length > 1
                        onClicked: {
                            root.syncChecking = true
                            root.syncStatus = "Playing test tones…"
                            syncCmdSource.disconnectSource(root.sourceCmd + " check-sync")
                            syncCmdSource.connectSource(root.sourceCmd + " check-sync")
                        }
                    }
                }

                PC3.Label {
                    Layout.fillWidth: true
                    visible: root.syncStatus !== ""
                    text: root.syncStatus
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    wrapMode: Text.Wrap
                }

                PC3.Label {
                    Layout.fillWidth: true
                    visible: (root.diagnostics.wireplumber_restarts || 0) > 0
                    text: "WirePlumber restarted at "
                        + (root.diagnostics.wireplumber_active_at || "unknown time")
                    color: Kirigami.Theme.neutralTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    wrapMode: Text.Wrap
                }

            }
        }

        // ── content ───────────────────────────────────────────────────────
        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // ── Outputs ───────────────────────────────────────────────────

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing

                PC3.Label {
                    text: "Output Devices"
                    color: Kirigami.Theme.disabledTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    font.bold: true
                }
                Kirigami.Separator {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            Repeater {
                model: root.sinks
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing

                    PC3.CheckBox {
                        checked: modelData.active
                        enabled: !modelData.active || root.sinks.filter(s => s.active).length > 1
                        onToggled: root.onSinkToggled(modelData.name, checked)
                    }

                    PC3.Label {
                        Layout.fillWidth: true
                        text: modelData.description
                        elide: Text.ElideRight
                    }

                    PC3.Label {
                        visible: modelData.role === "master" && modelData.active
                        text: "master"
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        color: Kirigami.Theme.disabledTextColor
                        font.italic: true
                    }

                    PC3.Slider {
                        id: sinkSlider
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 7
                        from: 0; to: 150; stepSize: 1
                        value: modelData.volume
                        Binding {
                            target: sinkSlider
                            property: "value"
                            value: modelData.volume
                            when: !sinkSlider.pressed
                        }
                        onPressedChanged: {
                            if (!pressed)
                                root.runCmd(root.sourceCmd + " set-sink-vol "
                                    + modelData.name + " " + Math.round(value))
                        }
                    }

                    PC3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.2
                        text: Math.round(sinkSlider.value) + "%"
                        horizontalAlignment: Text.AlignRight
                    }

                    PC3.Button {
                        text: "\u2212"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        enabled: modelData.offset > 0
                        onClicked: root.onSinkOffset(modelData.name, Math.max(0, modelData.offset - 15))
                    }

                    PC3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.8
                        text: (modelData.offset > 0 ? "+" : "") + modelData.offset + "ms"
                        horizontalAlignment: Text.AlignHCenter
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        opacity: 0.7
                    }

                    PC3.Button {
                        text: "+"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        onClicked: root.onSinkOffset(modelData.name, modelData.offset + 15)
                    }
                }
            }

            // ── Inputs ────────────────────────────────────────────────────

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.largeSpacing
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing

                PC3.Label {
                    text: "Input Devices"
                    color: Kirigami.Theme.disabledTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    font.bold: true
                }
                Kirigami.Separator {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            Repeater {
                model: root.sources
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing

                    PC3.CheckBox {
                        checked: modelData.active
                        enabled: !modelData.active || root.sources.filter(s => s.active).length > 1
                        onToggled: root.onSourceToggled(modelData.name, checked)
                    }

                    PC3.Label {
                        Layout.fillWidth: true
                        text: modelData.description
                        elide: Text.ElideRight
                    }

                    PC3.Slider {
                        id: sourceSlider
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 7
                        from: 0; to: 150; stepSize: 1
                        value: modelData.volume
                        Binding {
                            target: sourceSlider
                            property: "value"
                            value: modelData.volume
                            when: !sourceSlider.pressed
                        }
                        onPressedChanged: {
                            if (!pressed)
                                root.runCmd(root.sourceCmd + " set-source-vol "
                                    + modelData.name + " " + Math.round(value))
                        }
                    }

                    PC3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.2
                        text: Math.round(sourceSlider.value) + "%"
                        horizontalAlignment: Text.AlignRight
                    }

                    PC3.Button {
                        text: "\u2212"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        enabled: modelData.offset > 0
                        onClicked: root.onSourceOffset(modelData.name, Math.max(0, modelData.offset - 15))
                    }

                    PC3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.8
                        text: (modelData.offset > 0 ? "+" : "") + modelData.offset + "ms"
                        horizontalAlignment: Text.AlignHCenter
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        opacity: 0.7
                    }

                    PC3.Button {
                        text: "+"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        onClicked: root.onSourceOffset(modelData.name, modelData.offset + 15)
                    }
                }
            }

            Item { Layout.fillHeight: true }
        }
    }

    // ── data sources ──────────────────────────────────────────────────────

    Plasma5Support.DataSource {
        id: executableSource
        engine: "executable"
        interval: 0
        onNewData: (sourceName, sourceData) => {
            if (sourceName !== root.sourceCmd) return
            root.parseState(sourceData.stdout || "")
            executableSource.disconnectSource(sourceName)
        }
    }

    Plasma5Support.DataSource {
        id: cmdSource
        engine: "executable"
        interval: 0
        onNewData: (sourceName, sourceData) => {
            cmdSource.disconnectSource(sourceName)
            root.pollNow()
        }
    }

    Plasma5Support.DataSource {
        id: syncCmdSource
        engine: "executable"
        interval: 0
        onNewData: (sourceName, sourceData) => {
            syncCmdSource.disconnectSource(sourceName)
            root.syncChecking = false
            try {
                const stdout = sourceData.stdout || ""
                const stderr = sourceData.stderr || ""
                if (!stdout.trim()) {
                    root.syncStatus = "No response from daemon" + (stderr ? ": " + stderr.substring(0, 200) : "")
                    return
                }
                const r = JSON.parse(stdout)
                if (!r.ok) {
                    root.syncStatus = "Calibration error: " + (r.error || "unknown")
                    return
                }
                let lines = []
                let applied = 0
                let issues = 0
                for (const [sink, data] of Object.entries(r.results || {})) {
                    const dev = root.sinks.find(s => s.name === sink)
                    const label = dev ? dev.description : sink
                    if (data.error) {
                        lines.push(label + ": " + data.error)
                        issues++
                    } else if (data.confidence < 0.3) {
                        lines.push(label + ": no signal detected (conf " + Math.round(data.confidence * 100) + "%)")
                        issues++
                    } else if (data.delay_ms < 0) {
                        lines.push(label + ": invalid reading " + data.delay_ms + "ms (conf " + Math.round(data.confidence * 100) + "%)")
                        issues++
                    } else {
                        lines.push(label + ": " + data.delay_ms + "ms (conf " + Math.round(data.confidence * 100) + "%)")
                        applied++
                    }
                }
                let summary = ""
                if (applied > 0) summary = applied + " offset" + (applied > 1 ? "s" : "") + " applied."
                if (issues > 0) summary += (summary ? " " : "") + issues + " could not be measured."
                root.syncStatus = lines.join("\n") + (summary ? "\n" + summary : "")
            } catch(e) {
                root.syncStatus = "Parse error: " + e.toString()
            }
            root.pollNow()
        }
    }

    Timer {
        interval: root.refreshMs
        repeat: true
        running: root.expanded
        triggeredOnStart: false
        onTriggered: root.pollNow()
    }

    onExpandedChanged: {
        if (root.expanded) root.pollNow()
    }
}
