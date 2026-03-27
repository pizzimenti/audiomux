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

    readonly property string sourceCmd: "/home/bradley/bin/audiomux-source.py"
    property int refreshMs: 2000

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

    function parseState(json) {
        try {
            const s = JSON.parse(json)
            root.sinks   = s.sinks   || []
            root.sources = s.sources || []
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
        runCmd(root.sourceCmd + " set-sink-offset " + name + " " + offset)
    }

    function onSourceOffset(name, offset) {
        runCmd(root.sourceCmd + " set-source-offset " + name + " " + offset)
    }

    // ── compact: icon ─────────────────────────────────────────────────────

    compactRepresentation: Kirigami.Icon {
        source: "mixer-three-slider-symbolic"
        isMask: true
        active: root.expanded
        implicitWidth: Kirigami.Units.iconSizes.smallMedium
        implicitHeight: Kirigami.Units.iconSizes.smallMedium

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
            RowLayout {
                anchors.fill: parent

                Kirigami.Icon {
                    source: "mixer-three-slider-symbolic"
                    isMask: true
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium
                }

                PlasmaExtras.Heading {
                    level: 1
                    text: "AudioMux"
                    Layout.fillWidth: true
                    leftPadding: Kirigami.Units.smallSpacing
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

                    Item {
                        visible: modelData.is_primary
                        Layout.preferredWidth: Kirigami.Units.gridUnit * (1.4 + 2.8 + 1.4) + Kirigami.Units.smallSpacing * 2
                        height: 1
                    }

                    PC3.Button {
                        text: "−"
                        visible: !modelData.is_primary
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        onClicked: root.onSinkOffset(modelData.name, modelData.offset - 10)
                    }

                    PC3.Label {
                        visible: !modelData.is_primary
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.8
                        text: (modelData.offset > 0 ? "+" : "") + modelData.offset + "ms"
                        horizontalAlignment: Text.AlignHCenter
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        opacity: 0.7
                    }

                    PC3.Button {
                        text: "+"
                        visible: !modelData.is_primary
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
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

                    Item {
                        visible: modelData.is_primary
                        Layout.preferredWidth: Kirigami.Units.gridUnit * (1.4 + 2.8 + 1.4) + Kirigami.Units.smallSpacing * 2
                        height: 1
                    }

                    PC3.Button {
                        text: "−"
                        visible: !modelData.is_primary
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
                        onClicked: root.onSourceOffset(modelData.name, modelData.offset - 10)
                    }

                    PC3.Label {
                        visible: !modelData.is_primary
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2.8
                        text: (modelData.offset > 0 ? "+" : "") + modelData.offset + "ms"
                        horizontalAlignment: Text.AlignHCenter
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        opacity: 0.7
                    }

                    PC3.Button {
                        text: "+"
                        visible: !modelData.is_primary
                        implicitWidth: Kirigami.Units.gridUnit * 1.4
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
        interval: root.expanded ? root.refreshMs : 10000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.pollNow()
    }

    onExpandedChanged: {
        if (root.expanded) root.pollNow()
    }

    Component.onCompleted: pollNow()
}
