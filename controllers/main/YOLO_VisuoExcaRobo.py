import sys
from typing import Any, Tuple, List
from controller import Supervisor

try:
    import cv2
    import math
    import random
    import numpy as np
    import gymnasium as gym
    from ultralytics import YOLO
    from gymnasium import Env, spaces
    from gymnasium.envs.registration import EnvSpec, register
except ImportError:
    sys.exit(
        "Please make sure you have all dependencies installed. "
        "Run: `pip install -r requirements.txt`"
    )

# Constants used in the environment
ENV_ID = "YOLO_VisuoExcaRobo"
OBS_SPACE_SCHEMA = 1  # 1: coordinates of the target, 2: pure image
REWARD_SCHEMA = 2  # 1: reward function based on pixel position, 2: reward function based on distance
MAX_EPISODE_STEPS = 2000
MAX_WHEEL_SPEED = 5.0
MAX_MOTOR_SPEED = 0.7
MAX_ROBOT_DISTANCE = 8.0
TARGET_AREA_TH = 2000

# Constants for the logistic function
LOWER_Y = -38
STEPNESS = 5
MIDPOINT = 13
TARGET_TH = 3


class YOLO_VisuoExcaRobo(Supervisor, Env):
    """
    A custom Gym environment for controlling an excavator robot in Webots using YOLO-based target detection.

    This class integrates the Webots Supervisor with Gymnasium's Env, enabling reinforcement learning tasks.
    """

    def __init__(self, max_episode_steps: int = MAX_EPISODE_STEPS) -> None:
        """
        Initialize the YOLO_VisuoExcaRobo environment.

        Initializes the YOLOControl class, setting up the simulation environment,
        camera, motors, and YOLO model.

        Args:
            max_episode_steps (int): The maximum number of steps per episode.
        """
        super().__init__()
        self.timestep = int(self.getBasicTimeStep())
        random.seed(123)

        # Register the environment with Gym
        self.spec: EnvSpec = EnvSpec(id=ENV_ID, max_episode_steps=max_episode_steps)

        # Get the robot node
        self.robot = self.getFromDef("EXCAVATOR")

        # Set the observation and reward schemas
        self.obs_space_schema = OBS_SPACE_SCHEMA
        self.reward_schema = REWARD_SCHEMA
        self.target_area_th = TARGET_AREA_TH

        # Set motor and wheel speeds
        self.max_motor_speed = MAX_MOTOR_SPEED
        self.max_wheel_speed = MAX_WHEEL_SPEED
        self.max_robot_distance = MAX_ROBOT_DISTANCE

        # Set the logistic function parameters
        self.midpoint = MIDPOINT
        self.target_th = TARGET_TH

        # Get the floor node and set arena boundaries
        self.floor = self.getFromDef("FLOOR")
        self.set_arena_boundaries()

        # Initialize camera
        self.camera = self.init_camera()

        # Set camera properties
        self.camera_width, self.camera_height = (
            self.camera.getWidth(),
            self.camera.getHeight(),
        )
        self.frame_area = self.camera_width * self.camera_height

        # Target properties
        self.center_x = self.camera_width / 2
        self.lower_y = self.camera_height + LOWER_Y
        self.target_coordinate = [self.center_x, self.lower_y]
        self.tolerance_x = 1
        self.moiety = 2.0 * self.camera_height / 3.0 + 5

        # Load the YOLO model
        self.yolo_model = YOLO("../../runs/detect/train_m_300/weights/best.pt")

        # Create a window for displaying the processed image
        cv2.namedWindow("Webots YOLO Display", cv2.WINDOW_AUTOSIZE)

        # Define action space and observation space
        self.action_space = spaces.Box(low=-1, high=1, shape=(2,), dtype=np.float32)

        if self.obs_space_schema == 1:  # schema 1: coordinates of the target
            high = max(self.camera_width, self.camera_height)
            self.observation_space = spaces.Box(
                low=0, high=high, shape=(4,), dtype=np.uint16
            )

            # Initialize the robot state (schema 1)
            self.state = np.zeros(4, dtype=np.uint16)
        elif self.obs_space_schema == 2:  # schema 2: pure image
            self.observation_space = spaces.Box(
                low=0,
                high=255,
                shape=(3, self.camera_height, self.camera_width),
                dtype=np.uint8,
            )

            # Initialize the robot state (schema 2)
            self.state = np.zeros(
                (3, self.camera_height, self.camera_width), dtype=np.uint8
            )

        # Variables initialization
        self.prev_target_area = 0

    def reset(self, seed: Any = None, options: Any = None) -> Any:
        """
        Reset the environment to the initial state.

        Args:
            seed (Any): Seed for random number generation.
            options (Any): Additional options for reset.

        Returns:
            Tuple: Initial observation and info dictionary.
        """
        # Reset the simulation
        self.simulationReset()
        self.simulationResetPhysics()
        super().step(self.timestep)

        # Set robot to initial position
        self.init_pos = self.robot.getPosition()

        # Initialize motors and sensors
        self.wheel_motors, self.motors, self.sensors = self.init_motors_and_sensors()
        self.left_wheels = [self.wheel_motors["lf"], self.wheel_motors["lb"]]
        self.right_wheels = [self.wheel_motors["rf"], self.wheel_motors["rb"]]

        super().step(self.timestep)

        if self.obs_space_schema == 1:  # schema 1: coordinates of the target
            # Initialize state (schema 1)
            self.state = np.zeros(4, dtype=np.uint16)
        elif self.obs_space_schema == 2:  # schema 2: pure image
            # Initialize state (schema 2)
            self.state = np.zeros(
                (3, self.camera_height, self.camera_width), dtype=np.uint8
            )

        info: dict = {}

        return self.state, info

    def step(self, action) -> Tuple[np.ndarray, float, bool, bool, dict]:
        """
        Execute one step in the environment.

        Args:
            action (np.ndarray): The action to be taken by the robot.

        Returns:
            Tuple: Observation, reward, done flag, truncation flag, and info dictionary.
        """
        # Set the action for left and right wheels
        left_wheels_action = action[0] * self.max_wheel_speed
        right_wheels_action = action[1] * self.max_wheel_speed

        # Control the wheels
        self.run_wheels(left_wheels_action, "left")
        self.run_wheels(right_wheels_action, "right")

        # Proceed to the next simulation step
        super().step(self.timestep)

        # Get new observation
        target_coordinate, target_distance = self.get_observation()

        # Update the state based on the observation schema
        if self.obs_space_schema == 1:  # schema 1: coordinates of the target
            self.state = target_coordinate
        elif self.obs_space_schema == 2:  # schema 2: pure image
            image = self.camera.getImage()

            red_channel, green_channel, blue_channel = self.extract_rgb_channels(
                image, self.camera_width, self.camera_height
            )

            self.state = np.array(
                [red_channel, green_channel, blue_channel], dtype=np.uint8
            )

        # Calculate the reward and check if the episode is done
        if self.reward_schema == 1:  # schema 1: reward function based on pixel position
            reward, done, info = self.get_reward_and_done_1(target_coordinate)
        elif self.reward_schema == 2:  # schema 2: reward function based on distance
            reward, done, info = self.get_reward_and_done_2(target_distance)

        info["coordinates"] = target_coordinate

        return self.state, reward, done, False, info

    def render(self, mode: str = "human") -> Any:
        """
        Render the environment (not implemented).

        Args:
            mode (str): The mode for rendering.

        Returns:
            Any: Not used.
        """
        pass

    def seed(self, seed=None) -> List[int]:
        """
        Seed the environment for reproducibility.

        Args:
            seed (Any): The seed value.

        Returns:
            List[int]: The list containing the seed used.
        """
        self.np_random, seed = gym.utils.seeding.np_random(seed)
        return [seed]

    def get_reward_and_done_1(self, coordinate=[0, 0, 0, 0]) -> Tuple[float, bool]:
        """
        Schema 1: Reward Function based on the pixel position of the target.

        Args:
            coordinate (list): The coordinates of the target object.

        Returns:
            Tuple: The reward and done flag.
        """
        # Get the target coordinates and calculate the centroid
        target_x_min, target_y_min, target_x_max, target_y_max = coordinate
        centroid = [
            (target_x_min + target_x_max) / 2,
            (target_y_min + target_y_max) / 2,
        ]

        # Calculate the reward based on the target area
        target_area = (target_x_max - target_x_min) * (target_y_max - target_y_min)
        reward_area = 100 if target_area > self.prev_target_area else -100

        # Check if the robot reaches the target
        reach_target = target_area >= self.target_area_th
        reward_reach_target = 10000 if reach_target else 0

        # Reward based on pixel position (x and y alignment)
        in_target_x = (
            self.center_x - self.tolerance_x
            <= centroid[0]
            <= self.center_x + self.tolerance_x
        )
        in_target_y = centroid[1] >= self.moiety
        in_target = in_target_x and in_target_y

        # x-axis reward and punishment
        reward_x = 10000 if in_target_x else -10 * abs(centroid[0] - self.center_x)

        # y-axis reward and punishment
        reward_y = 10000 if in_target_y else -10 * (self.moiety - centroid[1])

        # Give The Punishment
        # Time Punishment
        time_punishment = -1

        # Check robot position relative to its initial position
        pos = self.robot.getPosition()
        robot_distance = (
            (pos[0] - self.init_pos[0]) ** 2 + (pos[1] - self.init_pos[1]) ** 2
        ) ** 0.5
        robot_far_away = robot_distance > self.max_robot_distance
        robot_distance_punishment = -10000 if robot_far_away else 0

        # Check if the robot hits the arena boundaries
        arena_th = 1.5
        hit_arena = not (
            self.arena_x_min + arena_th <= pos[0] <= self.arena_x_max - arena_th
            and self.arena_y_min + arena_th <= pos[1] <= self.arena_y_max - arena_th
        )
        hit_arena_punishment = -10000 if hit_arena else 0

        # Final reward calculation
        reward = (
            reward_area
            + reward_x
            + reward_y
            + reward_reach_target
            + time_punishment
            + robot_distance_punishment
            + hit_arena_punishment
        )

        # Check if the episode is done
        done = reach_target or robot_far_away or hit_arena or in_target

        # === testing purposes variables ===

        # Get the robot position
        x, y, _ = pos

        # get the deviation error  of x and y from the target
        deviation_x = abs(centroid[0] - self.center_x)
        deviation_y = abs(self.moiety - centroid[1])

        info = {
            "positions": (x, y),
            "deviation_x": deviation_x,
            "deviation_y": deviation_y,
            "target_area": target_area,
        }

        # Update the previous target area
        self.prev_target_area = target_area

        return reward, bool(done), info

    def get_reward_and_done_2(self, distance: float = 300) -> Tuple[float, bool]:
        """
        Schema 2: Reward Function based on the distance to the target and the robot's position.
        Calculate the reward and done flag based on the target area and robot position.

        Args:
            distance (float): The distance between the target point and the current object.

        Returns:
            Tuple: The reward and done flag.
        """
        # Calculate the reward based on the distance to the target
        reward_yolo = self.f(distance) * 0.001

        # Check if the robot reaches the target
        reach_target = 0 <= distance <= self.target_th
        reward_reach_target = 10 if reach_target else 0

        # Give The Punishment
        # Check robot position relative to its initial position
        pos = self.robot.getPosition()
        robot_distance = (
            (pos[0] - self.init_pos[0]) ** 2 + (pos[1] - self.init_pos[1]) ** 2
        ) ** 0.5
        robot_far_away = robot_distance > self.max_robot_distance
        robot_distance_punishment = -1 if robot_far_away else 0

        # Check if the robot hits the arena boundaries
        arena_th = 1.5
        hit_arena = not (
            self.arena_x_min + arena_th <= pos[0] <= self.arena_x_max - arena_th
            and self.arena_y_min + arena_th <= pos[1] <= self.arena_y_max - arena_th
        )
        hit_arena_punishment = -1 if hit_arena else 0

        # Final reward calculation
        reward = (
            reward_yolo
            + reward_reach_target
            + robot_distance_punishment
            + hit_arena_punishment
        )

        # Check if the episode is done
        done = reach_target or robot_far_away or hit_arena

        # === testing purposes variables ===

        # Get the robot position
        x, y, _ = pos

        info = {"positions": (x, y), "distance": distance}

        return reward, bool(done), info

    def f(
        self,
        x,
        stepness=STEPNESS,
        midpoint=MIDPOINT,
    ) -> float:
        """
        Calculate the reward based on the distance to the target using a logistic function.

        Args:
            x (float): Distance from the target.
            stepness (int): Sharpness factor for the logistic function.
            midpoint (float): Threshold distance for target detection.

        Returns:
            float: The reward based on the target distance.
        """
        exponent = ((stepness * x) - (stepness * midpoint)) * math.log(10)
        try:
            result = 1 / (1 + math.exp(exponent))
        except OverflowError:
            result = 0  # If exponent is too large, the value approaches zero

        return result

    def get_observation(self) -> Tuple[np.ndarray, float]:
        """
        Captures an image from the camera, performs object detection using YOLO,
        and processes the results to determine the target's position.

        Returns:
            Tuple: The current state and the distance to the target.
        """
        image = self.camera.getImage()

        # Convert image to NumPy array and then to BGR
        # img_np = np.frombuffer(image, dtype=np.uint8).reshape(
        #     (self.camera_height, self.camera_width, 4)
        # )
        # img_bgr = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)

        # Extract RGB channels from the image
        red_channel, green_channel, blue_channel = self.extract_rgb_channels(
            image, self.camera_width, self.camera_height
        )

        # Stack the channels together into a NumPy array
        img_bgr = np.stack((blue_channel, green_channel, red_channel), axis=-1)

        # Convert to a NumPy array with the correct dtype
        img_bgr = np.array(img_bgr, dtype=np.uint8)

        # Perform recognition
        coordinate, distance, label = self.recognition_process(img_bgr)

        # Get the image from the Webots camera (BGRA format)
        self._get_image_in_display(img_bgr, coordinate, label)

        return coordinate, distance

    def recognition_process(self, img_bgr):
        """
        Processes the image for object detection using the YOLO model.

        Args:
            img_bgr (np.ndarray): The BGR image to process.

        Returns:
            Tuple[np.ndarray, float, List[float], List[Any]]: The observation state, distance to the target, centroid of the target, and YOLO results.
        """
        distance, centroid = 300.0, [0, 0]
        # x_min, y_min, x_max, y_max = 0, 0, 0, 0
        # obs = np.zeros(4, dtype=np.uint16)
        cords, label, conf = np.zeros(4, dtype=np.uint16), "", 0.0

        # Perform object detection with YOLO
        results = self.yolo_model.predict(img_bgr, stream_buffer=True, verbose=False)
        result = results[0]

        # Post-process the results (shows only if the object is a rock)
        if result.boxes:
            for box in result.boxes:
                cords = box.xyxy[0].tolist()  # Get the coordinates
                cords = np.array(
                    [round(x) for x in cords], dtype=np.uint16
                )  # Round the coordinates
                label = result.names[box.cls[0].item()]  # Get the label
                conf = round(box.conf[0].item(), 2)  # Get the confidence

                if label == "rock":
                    # Get the coordinates of the bounding box
                    x_min, y_min, x_max, y_max = cords

                    # Get the new state
                    # obs = np.array([x_min, y_min, x_max, y_max], dtype=np.uint16)

                    # Calculate the centroid and the distance from the lower center
                    centroid = [(x_min + x_max) / 2, (y_min + y_max) / 2]
                    distance = np.sqrt(
                        (centroid[0] - self.target_coordinate[0]) ** 2
                        + (centroid[1] - self.target_coordinate[1]) ** 2
                    )
        else:
            cords = np.zeros(4, dtype=np.uint16)
            distance = 300.0
            label = ""

        return cords, distance, label

    def _get_image_in_display(self, img_bgr, coordinate, label):
        """
        Captures an image from the Webots camera and processes it for object detection.

        Returns:
            np.ndarray: The processed BGR image.
        """
        # Draw bounding box with label if state is not empty
        if np.any(coordinate):
            self.draw_bounding_box(img_bgr, coordinate, label)

        # Display the image in the OpenCV window
        cv2.imshow("Webots YOLO Display", img_bgr)
        cv2.waitKey(1)

    def draw_bounding_box(self, img, cords, label):
        """
        Draws a bounding box around the detected object and labels it.

        Args:
            img (np.ndarray): The image on which to draw the bounding box.
            cords (list): Coordinates of the bounding box.
            label (str): The label of the detected object.
        """
        bb_x_min, bb_y_min, bb_x_max, bb_y_max = cords

        # Draw the bounding box
        cv2.rectangle(
            img, (bb_x_min, bb_y_min), (bb_x_max, bb_y_max), (0, 0, 255), 2
        )  # Red box

        # Get the width and height of the text box
        (w, h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)

        # Draw a filled rectangle for the label background
        cv2.rectangle(
            img, (bb_x_min, bb_y_min - h - 1), (bb_x_min + w, bb_y_min), (0, 0, 255), -1
        )

        # Put the label text on the image
        cv2.putText(
            img,
            label,
            (bb_x_min, bb_y_min - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.3,
            (255, 255, 255),
            1,
        )

    def extract_rgb_channels(
        self, image, width, height
    ) -> Tuple[List[List[int]], List[List[int]], List[List[int]]]:
        """
        Extract the RGB channels from the camera image.

        Args:
            image (Any): The image captured by the camera.
            width (int): Width of the camera frame.
            height (int): Height of the camera frame.

        Returns:
            Tuple: Red, Green, and Blue channels as lists of lists.
        """
        red_channel, green_channel, blue_channel = [], [], []
        for j in range(height):
            red_row, green_row, blue_row = [], [], []
            for i in range(width):
                red_row.append(self.camera.imageGetRed(image, width, i, j))
                green_row.append(self.camera.imageGetGreen(image, width, i, j))
                blue_row.append(self.camera.imageGetBlue(image, width, i, j))
            red_channel.append(red_row)
            green_channel.append(green_row)
            blue_channel.append(blue_row)
        return red_channel, green_channel, blue_channel

    def set_arena_boundaries(self) -> None:
        """
        Set the boundaries of the arena based on the floor node size.
        """
        arena_tolerance = 1.0
        size_field = self.floor.getField("floorSize").getSFVec3f()
        x, y = size_field
        self.arena_x_max, self.arena_y_max = (
            x / 2 - arena_tolerance,
            y / 2 - arena_tolerance,
        )
        self.arena_x_min, self.arena_y_min = -self.arena_x_max, -self.arena_y_max

    def init_camera(self) -> Any:
        """
        Initialize the camera device and enable recognition.

        Returns:
            Any: The initialized camera device.
        """
        camera = self.getDevice("cabin_camera")
        camera.enable(self.timestep)
        camera.recognitionEnable(self.timestep)
        camera.enableRecognitionSegmentation()
        return camera

    def init_motors_and_sensors(self) -> Tuple[dict, dict, dict]:
        """
        Initialize the motors and sensors of the robot.

        Returns:
            Tuple: Dictionaries of wheel motors, arm motors, and sensors.
        """
        names = ["turret", "arm_connector", "lower_arm", "uppertolow", "scoop"]
        wheel = ["lf", "rf", "lb", "rb"]

        wheel_motors = {side: self.getDevice(f"wheel_{side}") for side in wheel}
        motors = {name: self.getDevice(f"{name}_motor") for name in names}
        sensors = {name: self.getDevice(f"{name}_sensor") for name in names}

        for motor in list(wheel_motors.values()) + list(motors.values()):
            motor.setPosition(float("inf"))
            motor.setVelocity(0.0)

        for sensor in sensors.values():
            sensor.enable(self.timestep)

        return wheel_motors, motors, sensors

    def run_wheels(self, velocity, wheel) -> None:
        """
        Set the velocity for the robot's wheels.

        Args:
            velocity (float): Speed to set for the wheels.
            wheel (str): Specifies which wheels to move ('left' or 'right').
        """
        wheels = self.left_wheels if wheel == "left" else self.right_wheels
        for motor in wheels:
            motor.setVelocity(velocity)


# Register the environment
register(
    id=ENV_ID,
    entry_point=lambda: YOLO_VisuoExcaRobo(),
    max_episode_steps=MAX_EPISODE_STEPS,
)
