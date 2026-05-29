# ui.py
import sys
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QTextEdit, QPushButton, QLineEdit

class AgentWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bioinformatics Agent")

        self.prompt = QLineEdit()
        self.prompt.setPlaceholderText("Enter task...")

        self.run_button = QPushButton("Run")
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addWidget(self.prompt)
        layout.addWidget(self.run_button)
        layout.addWidget(self.log)
        self.setLayout(layout)

        self.run_button.clicked.connect(self.run_agent)

    def run_agent(self):
        query = self.prompt.text()
        self.log.append(f"> {query}")
        self.log.append("Agent execution will connect here.")

app = QApplication(sys.argv)
window = AgentWindow()
window.resize(900, 600)
window.show()
sys.exit(app.exec())