const int pressurePin = A0;  // Analog pin
bool isRunning = false;     // Track whether data collection is active
String inputCommand = "";   // For storing Serial input

void setup() {
  Serial.begin(19600);
  Serial.println("Type 'start' to begin or 'stop' to stop pressure collection.");
}

void loop() {
  // Check for serial input to start/stop
  if (Serial.available() > 0) {
    char inChar = (char)Serial.read();
    if (inChar == '\n' || inChar == '\r') {
      inputCommand.trim(); // Remove any extra newline characters
      if (inputCommand.equalsIgnoreCase("start")) {
        isRunning = true;
        Serial.println("Pressure data collection STARTED.");
      } else if (inputCommand.equalsIgnoreCase("stop")) {
        isRunning = false;
        Serial.println("Pressure data collection STOPPED.");
      }
      inputCommand = "";  // Reset command buffer
    } else {
      inputCommand += inChar;
    }
  }

  // Collect pressure data if started
  if (isRunning) {
    int sensorValue = analogRead(pressurePin);
    float voltage = sensorValue * (5.0 / 1023.0); // Convert to voltage
    Serial.println(voltage);
    delay(1000);  // 1-second interval
  }
}
