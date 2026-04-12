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
    property bool reconnectPending: false
    property bool busy: reconnectPending
    property var reconnectSinkNames: []
    property var reconnectSourceNames: []
    property var diagnostics: ({})
    property var pendingSinkOffsets: ({})
    property var pendingSourceOffsets: ({})
    readonly property int pendingOffsetTimeoutMs: 3000

    property var sinks: []
    property var sources: []

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
        } catch(e) {}
    }

    function onSinkToggled(name, active) {
        const actives = root.sinks
            .filter(s => s.name === name ? active : s.active)
            .map(s => s.name)
        runCmd(root.sourceCmd + " set-active-sinks " + actives.join(","))
    }

    function onSourceToggled(name, active) {
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

    function reconnectAll() {
        if (root.busy)
            return
        root.reconnectSinkNames = root.sinks.filter(s => s.active).map(s => s.name)
        root.reconnectSourceNames = root.sources.filter(s => s.active).map(s => s.name)
        root.reconnectPending = true
        reconnectCooldown.restart()
        runCmd(root.sourceCmd + " reconnect-all")
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
                        id: reconnectButton
                        text: "Reconnect All"
                        icon.name: "view-refresh-symbolic"
                        enabled: !root.busy
                        onClicked: root.reconnectAll()
                    }
                }

                PC3.Label {
                    Layout.fillWidth: true
                    visible: (root.diagnostics.wireplumber_restarts || 0) > 0
                    text: "WirePlumber restarted at "
                        + (root.diagnostics.wireplumber_active_at || "unknown time")
                    color: "#ff8a65"
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
                    readonly property bool reconnectTarget: root.reconnectPending
                        && root.reconnectSinkNames.indexOf(modelData.name) !== -1
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing
                    opacity: reconnectTarget ? 0.45 : 1

                    Item {
                        implicitWidth: sinkCheck.implicitWidth
                        implicitHeight: sinkCheck.implicitHeight

                        PC3.CheckBox {
                            id: sinkCheck
                            anchors.centerIn: parent
                            checked: modelData.active
                            enabled: !root.busy
                                && (!modelData.active || root.sinks.filter(s => s.active).length > 1)
                            opacity: reconnectTarget ? 0.2 : 1
                            onToggled: root.onSinkToggled(modelData.name, checked)
                        }

                        Rectangle {
                            id: sinkGlow
                            anchors.centerIn: sinkCheck
                            width: sinkCheck.width + Kirigami.Units.smallSpacing * 4
                            height: sinkCheck.height + Kirigami.Units.smallSpacing * 4
                            radius: width / 2
                            color: "#ff7a59"
                            opacity: reconnectTarget ? 0.32 : 0
                            z: -1
                        }

                        ParallelAnimation {
                            running: reconnectTarget
                            loops: Animation.Infinite
                            NumberAnimation {
                                target: sinkCheck
                                property: "scale"
                                from: 1
                                to: 1.16
                                duration: 140
                            }
                            NumberAnimation {
                                target: sinkCheck
                                property: "opacity"
                                from: 0.2
                                to: 1
                                duration: 140
                            }
                            NumberAnimation {
                                target: sinkGlow
                                property: "scale"
                                from: 0.86
                                to: 1.28
                                duration: 180
                            }
                            NumberAnimation {
                                target: sinkGlow
                                property: "opacity"
                                from: 0.1
                                to: 0.45
                                duration: 180
                            }
                        }

                        Rectangle {
                            parent: rootLaserOverlay
                            visible: reconnectTarget && reconnectButton.visible && sinkCheck.visible
                            color: "#ff5630"
                            height: 3
                            radius: 2
                            antialiasing: true
                            readonly property point startPoint: reconnectButton.mapToItem(
                                rootLaserOverlay, reconnectButton.width, reconnectButton.height / 2)
                            readonly property point endPoint: sinkCheck.mapToItem(
                                rootLaserOverlay, 0, sinkCheck.height / 2)
                            readonly property real dx: endPoint.x - startPoint.x
                            readonly property real dy: endPoint.y - startPoint.y
                            width: Math.sqrt(dx * dx + dy * dy)
                            x: startPoint.x
                            y: startPoint.y - height / 2
                            transformOrigin: Item.Left
                            rotation: Math.atan2(dy, dx) * 180 / Math.PI
                            opacity: reconnectTarget ? 0.9 : 0

                            SequentialAnimation on opacity {
                                running: reconnectTarget
                                loops: Animation.Infinite
                                NumberAnimation { from: 0.15; to: 1; duration: 90 }
                                NumberAnimation { from: 1; to: 0.35; duration: 120 }
                                PauseAnimation { duration: 80 }
                            }
                        }
                    }

                    PC3.Label {
                        Layout.fillWidth: true
                        text: modelData.description
                        elide: Text.ElideRight
                    }

                    PC3.Slider {
                        id: sinkSlider
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 7
                        from: 0; to: 150; stepSize: 1
                        value: modelData.volume
                        enabled: !root.busy
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
                        text: "−"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        enabled: !root.busy
                        onClicked: root.onSinkOffset(modelData.name, modelData.offset - 10)
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
                        enabled: !root.busy
                        onClicked: root.onSinkOffset(modelData.name, modelData.offset + 10)
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
                    readonly property bool reconnectTarget: root.reconnectPending
                        && root.reconnectSourceNames.indexOf(modelData.name) !== -1
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing
                    opacity: reconnectTarget ? 0.45 : 1

                    Item {
                        implicitWidth: sourceCheck.implicitWidth
                        implicitHeight: sourceCheck.implicitHeight

                        PC3.CheckBox {
                            id: sourceCheck
                            anchors.centerIn: parent
                            checked: modelData.active
                            enabled: !root.busy
                                && (!modelData.active || root.sources.filter(s => s.active).length > 1)
                            opacity: reconnectTarget ? 0.2 : 1
                            onToggled: root.onSourceToggled(modelData.name, checked)
                        }

                        Rectangle {
                            id: sourceGlow
                            anchors.centerIn: sourceCheck
                            width: sourceCheck.width + Kirigami.Units.smallSpacing * 4
                            height: sourceCheck.height + Kirigami.Units.smallSpacing * 4
                            radius: width / 2
                            color: "#ff7a59"
                            opacity: reconnectTarget ? 0.32 : 0
                            z: -1
                        }

                        ParallelAnimation {
                            running: reconnectTarget
                            loops: Animation.Infinite
                            NumberAnimation {
                                target: sourceCheck
                                property: "scale"
                                from: 1
                                to: 1.16
                                duration: 140
                            }
                            NumberAnimation {
                                target: sourceCheck
                                property: "opacity"
                                from: 0.2
                                to: 1
                                duration: 140
                            }
                            NumberAnimation {
                                target: sourceGlow
                                property: "scale"
                                from: 0.86
                                to: 1.28
                                duration: 180
                            }
                            NumberAnimation {
                                target: sourceGlow
                                property: "opacity"
                                from: 0.1
                                to: 0.45
                                duration: 180
                            }
                        }

                        Rectangle {
                            parent: rootLaserOverlay
                            visible: reconnectTarget && reconnectButton.visible && sourceCheck.visible
                            color: "#ff5630"
                            height: 3
                            radius: 2
                            antialiasing: true
                            readonly property point startPoint: reconnectButton.mapToItem(
                                rootLaserOverlay, reconnectButton.width, reconnectButton.height / 2)
                            readonly property point endPoint: sourceCheck.mapToItem(
                                rootLaserOverlay, 0, sourceCheck.height / 2)
                            readonly property real dx: endPoint.x - startPoint.x
                            readonly property real dy: endPoint.y - startPoint.y
                            width: Math.sqrt(dx * dx + dy * dy)
                            x: startPoint.x
                            y: startPoint.y - height / 2
                            transformOrigin: Item.Left
                            rotation: Math.atan2(dy, dx) * 180 / Math.PI
                            opacity: reconnectTarget ? 0.9 : 0

                            SequentialAnimation on opacity {
                                running: reconnectTarget
                                loops: Animation.Infinite
                                NumberAnimation { from: 0.15; to: 1; duration: 90 }
                                NumberAnimation { from: 1; to: 0.35; duration: 120 }
                                PauseAnimation { duration: 80 }
                            }
                        }
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
                        enabled: !root.busy
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
                        text: "−"
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        enabled: !root.busy
                        onClicked: root.onSourceOffset(modelData.name, modelData.offset - 10)
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
                        enabled: !root.busy
                        onClicked: root.onSourceOffset(modelData.name, modelData.offset + 10)
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

    Timer {
        interval: root.refreshMs
        repeat: true
        running: root.expanded || root.reconnectPending
        triggeredOnStart: false
        onTriggered: root.pollNow()
    }

    Timer {
        id: reconnectCooldown
        interval: 1000
        repeat: false
        onTriggered: {
            root.reconnectPending = false
            root.reconnectSinkNames = []
            root.reconnectSourceNames = []
        }
    }

    onExpandedChanged: {
        if (root.expanded) root.pollNow()
    }

    Item {
        id: rootLaserOverlay
        anchors.fill: parent
        z: 1000
        enabled: false
    }
}
